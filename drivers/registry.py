"""Driver registry factory mapping target configuration to L1 and L2 module instances."""
import tomllib
from typing import Any, Tuple
import structlog

from layer1.base import EnvironmentDriver
from layer1.tn3270 import TN3270Driver
from layer2.base import StateReducer
from layer2.reducer import TN3270StateReducer
from layer2.stability import OIAStabilityEngine

logger = structlog.get_logger(__name__)


class DriverRegistry:
    """Registry factory for instantiating target-specific L1 Driver, L2 Reducer, and Stability Engine."""

    @staticmethod
    def load_config(config_path: str) -> dict[str, Any]:
        """Load and parse TOML configuration file."""
        with open(config_path, "rb") as f:
            return tomllib.load(f)

    @classmethod
    def create_target(
        cls, config_path: str
    ) -> Tuple[EnvironmentDriver, StateReducer, OIAStabilityEngine, dict[str, Any]]:
        """Instantiate L1 Driver, L2 Reducer, and Stability Engine from TOML config."""
        config = cls.load_config(config_path)
        target_cfg = config.get("target", {})
        driver_type = target_cfg.get("driver", "tn3270").lower()

        if driver_type == "tn3270":
            tn3270_cfg = config.get("tn3270", {})
            driver = TN3270Driver(
                host=tn3270_cfg.get("host", "127.0.0.1"),
                port=tn3270_cfg.get("port", 3270),
                device_type=tn3270_cfg.get("device_type", "IBM-3278-2"),
                use_tls=tn3270_cfg.get("use_tls", False),
                runtime_id=target_cfg.get("runtime_id", "mainframe_node_01"),
            )
            reducer = TN3270StateReducer(rows=24, cols=80)

            stab_cfg = config.get("stability", {})
            stability = OIAStabilityEngine(
                poll_interval_ms=stab_cfg.get("poll_interval_ms", 50),
                settle_ms=stab_cfg.get("settle_ms", 100),
                max_wait_ms=stab_cfg.get("max_wait_ms", 10000),
                quiescence_ms=stab_cfg.get("quiescence_ms", 150),
            )
            return driver, reducer, stability, config
        else:
            raise NotImplementedError(f"Driver type '{driver_type}' is not registered yet.")

    @classmethod
    def create_action_lowerer(cls, driver_type: str = "tn3270"):
        """Instantiate target-specific ActionLowerer."""
        if driver_type.lower() == "tn3270":
            from layer2.action_lowerer import TN3270ActionLowerer
            return TN3270ActionLowerer()
        else:
            raise NotImplementedError(f"Action lowerer for '{driver_type}' is not registered yet.")

    @classmethod
    def create_gateway(cls, config_path: str):
        """Instantiate complete L1-L2-L3 pipeline and return (driver, gateway, config)."""
        from layer3.dispatcher import ActionDispatcher
        from layer3.gateway import AIToolGateway
        from schemas.contracts import L2toL3HandoffPayload

        driver, reducer, stability, config = cls.create_target(config_path)
        driver_type = config.get("target", {}).get("driver", "tn3270")
        lowerer = cls.create_action_lowerer(driver_type)

        dispatcher = ActionDispatcher(
            driver=driver,
            reducer=reducer,
            action_lowerer=lowerer,
            stability_engine=stability,
        )

        return driver, reducer, stability, lowerer, dispatcher, config
