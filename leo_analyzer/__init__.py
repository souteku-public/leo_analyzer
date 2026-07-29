"""LEO satellite link throughput measurement and antenna telemetry logger."""

__version__ = "0.9.0"


def build_id() -> str:
    """Version plus the git commit, when the tool runs from a checkout.

    Recorded into every summary.json so a data set always says which
    build produced it.
    """
    import subprocess
    from pathlib import Path

    try:
        commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).resolve().parent.parent,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if commit.returncode == 0 and commit.stdout.strip():
            return f"{__version__}+{commit.stdout.strip()}"
    except Exception:
        pass
    return __version__
