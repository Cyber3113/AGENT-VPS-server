import subprocess


def run_command(command: list, cwd: str = None, timeout: int = 600):
    """
    Serverda buyruq bajarish.

    Args:
        command: ["npm", "install"]
        cwd: Ishchi katalog
        timeout: Maksimal kutish vaqti (sekund)

    Returns:
        {
            success: bool,
            stdout: str,
            stderr: str,
            returncode: int
        }
    """

    try:

        result = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout
        )

        return {
            "success": result.returncode == 0,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
            "returncode": result.returncode
        }

    except subprocess.TimeoutExpired:

        return {
            "success": False,
            "stdout": "",
            "stderr": f"Command timeout ({timeout}s)",
            "returncode": -1
        }

    except FileNotFoundError:

        return {
            "success": False,
            "stdout": "",
            "stderr": f"Command topilmadi: {command[0]}",
            "returncode": -1
        }

    except Exception as e:

        return {
            "success": False,
            "stdout": "",
            "stderr": str(e),
            "returncode": -1
        }