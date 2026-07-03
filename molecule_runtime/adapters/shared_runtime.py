"""Re-export from shared_runtime for backward compat."""
from molecule_runtime.shared_runtime import *  # noqa: F401,F403

# The shared subprocess-executor base (history-injection + stable session-id
# contract) lives in its own module; re-export it here so subprocess runtime
# adapters can pull it from the same place as the shared helpers.
from molecule_runtime.subprocess_executor import (  # noqa: F401
    SubprocessA2AExecutor,
)
