import logging
import math
import pickle
import random
from typing import Iterable, Sequence, Tuple
from unittest.mock import Mock

from envision.client import Client as Envision
from smarts.core import seed as random_seed
from smarts.core.agent import Agent
from smarts.core.agent_interface import AgentInterface, AgentType
from smarts.core.scenario import Scenario
from smarts.core.sensors import Observation
from smarts.core.smarts import SMARTS
from smarts.core.traffic_history import TrafficHistory
from smarts.core.traffic_history_provider import TrafficHistoryProvider
from smarts.core.utils.math import rounder_for_dt
from smarts.zoo.agent_spec import AgentSpec

try:
    from argument_parser import default_argument_parser
except ImportError:
    from .argument_parser import default_argument_parser

logging.basicConfig(level=logging.INFO)


class ReplayCheckerAgent(Agent):
    """This is just a place holder such that the example code here has a real Agent to work with.
    This agent checks that the action space is working 'as expected'.
    In actual use, this would be replaced by an agent based on a trained Imitation Learning model."""

    def __init__(self, fixed_timestep_sec: float):
        self._fixed_timestep_sec = fixed_timestep_sec
        self._rounder = rounder_for_dt(fixed_timestep_sec)
        self._time_offset = 0
        self._data = None
        self._vehicle_id = ""

    def load_data_for_vehicle(
        self, vehicle_id: str, scenario: Scenario, time_offset: float
    ):
        self._vehicle_id = vehicle_id  # for debugging
        self._time_offset = time_offset

        datafile = f"collected_observations/{scenario.name}_{scenario.traffic_history.name}_Agent-history-vehicle-{vehicle_id}.pkl"
        # We read actions from a datafile previously-generated by the
        # observation_collection_for_imitation_learning.py script.
        # This allows us to test the action space to ensure that it
        # can recreate the original behaviour.
        with open(datafile, "rb") as pf:
            self._data = pickle.load(pf)

    def act(self, obs: Observation) -> Tuple[float, float]:
        assert self._data

        # First, check the observations representing the current state
        # to see if it matches what we expected from the recorded data.
        obs_time = self._rounder(obs.elapsed_sim_time + self._time_offset)
        exp = self._data.get(obs_time)
        if not exp:
            return (0.0, 0.0)
        cur_state = obs.ego_vehicle_state

        assert math.isclose(
            cur_state.heading, exp["heading"], abs_tol=1e-2
        ), f'vid={self._vehicle_id}: {cur_state.heading} != {exp["heading"]} @ {obs_time}'
        # Note: the other checks can't be as tight b/c we lose some accuracy (due to angular acceleration)
        # by converting the acceleration vector to a scalar in the observation script,
        # which compounds over time throughout the simulation.
        assert math.isclose(
            cur_state.speed, exp["speed"], abs_tol=0.2
        ), f'vid={self._vehicle_id}: {cur_state.speed} != {exp["speed"]} @ {obs_time}'
        assert math.isclose(
            cur_state.position[0], exp["ego_pos"][0], abs_tol=2
        ), f'vid={self._vehicle_id}: {cur_state.position[0]} != {exp["ego_pos"][0]} @ {obs_time}'
        assert math.isclose(
            cur_state.position[1], exp["ego_pos"][1], abs_tol=2
        ), f'vid={self._vehicle_id}: {cur_state.position[1]} != {exp["ego_pos"][1]} @ {obs_time}'

        # Then get and return the next set of control inputs
        atime = self._rounder(obs_time + self._fixed_timestep_sec)
        data = self._data.get(atime, {"acceleration": 0, "angular_velocity": 0})
        return (data["acceleration"], data["angular_velocity"])


def main(
    script: str,
    scenarios: Sequence[str],
    headless: bool,
    seed: int,
    vehicles_to_replace: int,
    episodes: int,
    exists_at_or_after: float = 40,
    minimum_history_duration: float = 10,
    ends_before: float = 80,
):
    assert vehicles_to_replace > 0
    assert episodes > 0
    logger = logging.getLogger(script)
    logger.setLevel(logging.INFO)

    logger.debug("initializing SMARTS")

    smarts = SMARTS(
        agent_interfaces={},
        traffic_sim=None,
        envision=None if headless else Envision(),
    )
    random_seed(seed)
    traffic_history_provider = smarts.get_provider_by_type(TrafficHistoryProvider)
    assert traffic_history_provider

    scenario_list = Scenario.get_scenario_list(scenarios)
    scenarios_iterator = Scenario.variations_for_all_scenario_roots(scenario_list, [])

    for scenario in scenarios_iterator:
        assert isinstance(scenario.traffic_history, TrafficHistory)
        logger.info("working on scenario {}".format(scenario.traffic_history.name))

        VehicleWindow = TrafficHistory.TrafficHistoryVehicleWindow
        # Can use this to further filter out prospective vehicles
        def custom_filter(vehs: Iterable[VehicleWindow]) -> Iterable[VehicleWindow]:
            nonlocal exists_at_or_after
            vehicles = list(vehs)
            logger.info(f"Total vehicles pre-filter: {len(vehicles)}")
            start_window = 4
            vehicles = list(
                v
                for v in vehicles
                if v.average_speed > 3
                and abs(v.start_time - exists_at_or_after) < start_window
            )
            logger.info(f"Total vehicles post-filter: {len(vehicles)}")
            return vehicles

        last_seen_vehicle_time = scenario.traffic_history.last_seen_vehicle_time()
        if last_seen_vehicle_time is None:
            logger.warning(
                f"no vehicles are found in `{scenario.traffic_history.name}` traffic history!!!"
            )

        logger.info(f"final vehicle exits at: {last_seen_vehicle_time}")

        # pytype: disable=attribute-error
        veh_missions = {
            mission.vehicle_spec.veh_id: mission
            for mission in scenario.history_missions_for_window(
                exists_at_or_after, ends_before, minimum_history_duration, custom_filter
            )
        }
        # pytype: enable=attribute-error
        if not veh_missions:
            logger.warning(
                "no vehicle missions found for scenario {}.".format(scenario.name)
            )
            continue

        k = vehicles_to_replace
        if k > len(veh_missions):
            logger.warning(
                "vehicles_to_replace={} is greater than the number of vehicle missions ({}).".format(
                    vehicles_to_replace, len(veh_missions)
                )
            )
            k = len(veh_missions)

        # XXX replace with AgentSpec appropriate for IL model
        agent_spec = AgentSpec(
            interface=AgentInterface.from_type(AgentType.Imitation),
            agent_builder=ReplayCheckerAgent,
            agent_params=smarts.fixed_timestep_sec,
        )

        for episode in range(episodes):
            logger.info(f"starting episode {episode}...")
            agentid_to_vehid = {}
            agent_interfaces = {}

            # Build the Agents for the to-be-hijacked vehicles
            # and gather their missions
            agents = {}
            dones = {}
            ego_missions = {}
            sample = set()

            if scenario.traffic_history.dataset_source == "Waymo":
                # For Waymo, we only hijack the vehicle that was autonomous in the dataset
                waymo_ego_id = scenario.traffic_history.ego_vehicle_id
                if waymo_ego_id is not None:
                    assert (
                        k == 1
                    ), f"do not specify -k > 1 when just hijacking Waymo ego vehicle (it was {k})"
                    veh_id = str(waymo_ego_id)
                    sample = {veh_id}
                else:
                    logger.warning(
                        f"Waymo ego vehicle id not mentioned in the dataset. Hijacking a random vehicle."
                    )

            if not sample:
                # For other datasets, hijack a sample of the recorded vehicles
                # Pick k vehicle missions to hijack with agent
                sample = set(random.sample(tuple(veh_missions.keys()), k))

            agent_spec.interface.max_episode_steps = max(
                [
                    scenario.traffic_history.vehicle_final_exit_time(veh_id) / 0.1
                    for veh_id in sample
                ]
            )
            history_start_time = None
            logger.info(f"chose vehicles: {sample}")
            for veh_id in sample:
                agent_id = f"ego-agent-IL-{veh_id}"
                agentid_to_vehid[agent_id] = veh_id
                agent_interfaces[agent_id] = agent_spec.interface
                if (
                    not history_start_time
                    or veh_missions[veh_id].start_time < history_start_time
                ):
                    history_start_time = veh_missions[veh_id].start_time

            for agent_id in agent_interfaces.keys():
                agent = agent_spec.build_agent()
                veh_id = agentid_to_vehid[agent_id]
                load_data_for_vehicle = getattr(agent, "load_data_for_vehicle", Mock())
                load_data_for_vehicle(veh_id, scenario, history_start_time)
                agents[agent_id] = agent
                dones[agent_id] = False
                ego_missions[agent_id] = veh_missions[veh_id]

            scenario.set_ego_missions(ego_missions)

            # Take control of vehicles with corresponding agent_ids
            smarts.switch_ego_agents(agent_interfaces)

            # Finally start the simulation loop...
            logger.info(
                f"mission start times: {[(veh_id, veh_missions[veh_id].start_time) for veh_id in sample]}"
            )
            logger.info(f"starting simulation loop at: `{history_start_time}`...")
            # Simulation time ticks over before capture, need to start 1 step before
            sim_start_time = max(0, history_start_time - smarts.fixed_timestep_sec)
            observations = smarts.reset(scenario, sim_start_time)
            assert smarts.elapsed_sim_time == smarts.fixed_timestep_sec or math.isclose(
                smarts.elapsed_sim_time, history_start_time
            ), f"{smarts.elapsed_sim_time} != {history_start_time}"
            while not all(done for done in dones.values()):
                actions = {
                    agent_id: agents[agent_id].act(agent_obs)
                    for agent_id, agent_obs in observations.items()
                }
                logger.debug(
                    "stepping @ sim_time={} for agents={}...".format(
                        smarts.elapsed_sim_time, list(observations.keys())
                    )
                )
                observations, rewards, dones, infos = smarts.step(actions)

                for agent_id in agents.keys():
                    if dones.get(agent_id, False):
                        if not observations[agent_id].events.reached_goal:
                            logger.warning(
                                "agent_id={} exited @ sim_time={}".format(
                                    agent_id, smarts.elapsed_sim_time
                                )
                            )
                            logger.warning(
                                "   ... with {}".format(observations[agent_id].events)
                            )
                        else:
                            logger.info(
                                "agent_id={} reached goal @ sim_time={}".format(
                                    agent_id, smarts.elapsed_sim_time
                                )
                            )
                            logger.debug(
                                "   ... with {}".format(observations[agent_id].events)
                            )
                        del observations[agent_id]

    smarts.destroy()


if __name__ == "__main__":
    parser = default_argument_parser("history-vehicles-replacement-example")
    parser.add_argument(
        "--replacements-per-episode",
        "-k",
        help="The number vehicles to randomly replace with agents per episode.",
        type=int,
        default=1,
    )
    args = parser.parse_args()

    main(
        script=parser.prog,
        scenarios=args.scenarios,
        headless=args.headless,
        seed=args.seed,
        vehicles_to_replace=args.replacements_per_episode,
        episodes=args.episodes,
    )
