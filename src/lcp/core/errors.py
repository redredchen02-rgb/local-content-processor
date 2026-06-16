"""Error hierarchy. Each error carries an exit_code so CLI/GUI shells read it
instead of branching on error type (keeps shell logic out of business code)."""

# Exit code contract (see plan R31)
EXIT_OK = 0
EXIT_USAGE = 1
EXIT_INPUT = 2
EXIT_DEPENDENCY = 3
EXIT_EXTERNAL = 4
EXIT_INTERNAL = 5


class LcpError(Exception):
    """Base error. exit_code is the process exit status the CLI should return."""

    exit_code = EXIT_INTERNAL


class UsageError(LcpError):
    exit_code = EXIT_USAGE


class InputValidationError(LcpError):
    """Bad user input: invalid URL, missing job, malformed config value."""

    exit_code = EXIT_INPUT


class DependencyError(LcpError):
    """Missing local dependency: ffmpeg/ffprobe absent, api_key not configured."""

    exit_code = EXIT_DEPENDENCY


class ExternalServiceError(LcpError):
    """External call failed: LLM timeout/5xx, network fetch error."""

    exit_code = EXIT_EXTERNAL
