import json
import os
import re
import shutil
import socket
import subprocess
from pathlib import Path

import requests

from utils import run_command


# Let's Encrypt uchun email (bo'sh bo'lsa emailsiz ro'yxatdan o'tadi)
LETSENCRYPT_EMAIL = os.environ.get("LETSENCRYPT_EMAIL", "").strip()

# .env fayl namunasi sifatida qabul qilinadigan fayl nomlari
ENV_EXAMPLE_NAMES = (
    ".env.example",
    ".env.sample",
    ".env.dist",
    ".env.template",
    "env.example",
    "env.sample",
)


def build_result(success: bool, msg: str, **extra):
    payload = {
        "status": success,
        "success": success,
        "msg": msg,
    }
    payload.update(extra)
    return payload


def normalize_path(path: str):
    return str(Path(path).expanduser().resolve())


def is_ignored_path(path: Path):
    ignored_parts = {".venv", "venv", "env", "site-packages", "__pycache__"}
    return any(part in ignored_parts for part in path.parts)


def find_first_file(base: Path, filename: str):
    direct = base / filename
    if direct.exists():
        return direct

    candidates = [
        item for item in base.rglob(filename)
        if item.is_file() and not is_ignored_path(item)
    ]

    if not candidates:
        return None

    candidates.sort(key=lambda item: (len(item.parts), str(item)))
    return candidates[0]


def find_python_bin(project_root: Path):
    candidates = [
        project_root / ".venv" / "bin" / "python",
        project_root / "venv" / "bin" / "python",
        project_root / "env" / "bin" / "python",
    ]

    for candidate in candidates:
        if candidate.exists():
            return str(candidate)

    return shutil.which("python3") or shutil.which("python") or "python3"


def find_env_example(base: Path):
    for name in ENV_EXAMPLE_NAMES:
        found = find_first_file(base, name)
        if found:
            return found
    return None


def ensure_env_file(base: Path):
    """
    .env faylini topadi. Agar .env bo'lmasa, lekin .env.example (yoki shunga
    o'xshash namuna) mavjud bo'lsa, uni avtomatik .env ga nusxalaydi.
    """
    env_file = find_first_file(base, ".env")
    if env_file and env_file.exists():
        return env_file, False

    example = find_env_example(base)
    if example and example.exists():
        target = example.parent / ".env"
        if not target.exists():
            shutil.copy(example, target)
        return target, True

    return None, False


def find_requirements_file(base: Path):
    return find_first_file(base, "requirements.txt")


def find_existing_venv(venv_parent: Path):
    for name in (".venv", "venv", "env"):
        candidate = venv_parent / name / "bin" / "python"
        if candidate.exists():
            return candidate.parent.parent
    return None


def ensure_virtualenv(base: Path):
    """
    requirements.txt joylashgan papkada virtual muhit yaratadi (yoki mavjudini
    ishlatadi), kutubxonalarni o'rnatadi va gunicorn/uvicorn ni qo'shadi.
    Django deploy uchun kerakli python_bin va gunicorn_bin ni qaytaradi.
    """
    logs = []

    requirements = find_requirements_file(base)
    venv_parent = requirements.parent if requirements else base

    venv_dir = find_existing_venv(venv_parent) or find_existing_venv(base)
    if venv_dir is None:
        venv_dir = venv_parent / "venv"

    python_bin = venv_dir / "bin" / "python"

    if not python_bin.exists():
        create = run_command(["python3", "-m", "venv", str(venv_dir)], timeout=300)
        if not create["success"]:
            return build_result(
                False,
                "❌ Virtual muhit yaratilmadi",
                stderr=create["stderr"],
                stdout=create["stdout"],
            )
        logs.append(f"✅ Virtual muhit yaratildi: {venv_dir}")
    else:
        logs.append(f"✅ Mavjud virtual muhit topildi: {venv_dir}")

    python_bin_str = str(python_bin)

    # pip/setuptools/wheel ni yangilash (xatolik bo'lsa ham to'xtamaymiz)
    run_command(
        [python_bin_str, "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel"],
        timeout=600,
    )

    if requirements:
        install = run_command(
            [python_bin_str, "-m", "pip", "install", "-r", str(requirements)],
            timeout=1800,
        )
        if not install["success"]:
            return build_result(
                False,
                "❌ requirements.txt o'rnatilmadi",
                logs=logs,
                stderr=install["stderr"],
                stdout=install["stdout"],
            )
        logs.append(f"✅ Kutubxonalar o'rnatildi: {requirements}")
    else:
        logs.append("⚠️ requirements.txt topilmadi, faqat server paketlari o'rnatiladi")

    # WSGI (gunicorn) va ASGI/websocket (uvicorn) uchun server paketlari
    server = run_command(
        [python_bin_str, "-m", "pip", "install", "gunicorn", "uvicorn[standard]"],
        timeout=900,
    )
    if not server["success"]:
        return build_result(
            False,
            "❌ gunicorn/uvicorn o'rnatilmadi",
            logs=logs,
            stderr=server["stderr"],
            stdout=server["stdout"],
        )
    logs.append("✅ gunicorn va uvicorn o'rnatildi")

    return build_result(
        True,
        "Virtual muhit tayyor",
        venv_dir=str(venv_dir),
        python_bin=python_bin_str,
        gunicorn_bin=str(venv_dir / "bin" / "gunicorn"),
        requirements=str(requirements) if requirements else "",
        logs=logs,
    )


def parse_settings_module(manage_text: str):
    match = re.search(r"DJANGO_SETTINGS_MODULE[^'\"]*['\"]([^'\"]+)['\"]", manage_text)
    if match:
        return match.group(1)
    return None


def resolve_django_project(path: str):
    base = Path(normalize_path(path))

    if not base.exists():
        return build_result(False, "❌ Path topilmadi")

    manage_py = find_first_file(base, "manage.py")
    if not manage_py:
        return build_result(False, "❌ manage.py topilmadi")

    try:
        manage_text = manage_py.read_text(encoding="utf-8", errors="ignore")
    except Exception as exc:
        return build_result(False, f"manage.py o'qib bo'lmadi: {exc}")

    settings_module = parse_settings_module(manage_text)
    settings_file = None
    project_dir = manage_py.parent

    if settings_module:
        settings_candidate = project_dir.joinpath(*settings_module.split(".")).with_suffix(".py")
        if settings_candidate.exists():
            settings_file = settings_candidate

    if settings_file is None:
        settings_file = find_first_file(project_dir, "settings.py")
        if not settings_file:
            return build_result(False, "❌ settings.py topilmadi")
        rel_settings = settings_file.relative_to(project_dir).with_suffix("")
        settings_module = ".".join(rel_settings.parts)

    project_module = settings_module.rsplit(".", 1)[0] if "." in settings_module else settings_file.parent.name
    wsgi_module = f"{project_module}.wsgi"
    wsgi_file = project_dir.joinpath(*wsgi_module.split(".")).with_suffix(".py")

    asgi_module = f"{project_module}.asgi"
    asgi_file = project_dir.joinpath(*asgi_module.split(".")).with_suffix(".py")

    if not wsgi_file.exists() and not asgi_file.exists():
        return build_result(False, "❌ wsgi.py yoki asgi.py topilmadi")

    # .env ni loyiha ildizidan (base) qidiramiz, kerak bo'lsa .env.example dan yaratamiz
    env_file, env_created = ensure_env_file(base)

    return build_result(
        True,
        "Django project topildi",
        base_root=str(base),
        project_root=str(project_dir),
        manage_py=str(manage_py),
        settings_file=str(settings_file),
        settings_module=settings_module,
        project_module=project_module,
        wsgi_module=wsgi_module,
        wsgi_file=str(wsgi_file) if wsgi_file.exists() else "",
        asgi_module=asgi_module,
        asgi_file=str(asgi_file) if asgi_file.exists() else "",
        env_file=str(env_file) if env_file else "",
        env_created=env_created,
        python_bin=find_python_bin(project_dir),
    )


def inspect_django_settings(project_root: str, python_bin: str):
    script = """
import json
from django.conf import settings

default_db = settings.DATABASES.get("default", {})
installed_apps = [str(app) for app in getattr(settings, "INSTALLED_APPS", [])]
asgi_application = str(getattr(settings, "ASGI_APPLICATION", "") or "")
channels_layers = bool(getattr(settings, "CHANNEL_LAYERS", None))
has_channels = any(app.split(".")[0] in ("channels", "daphne") for app in installed_apps)
payload = {
    "static_root": str(getattr(settings, "STATIC_ROOT", "") or ""),
    "media_root": str(getattr(settings, "MEDIA_ROOT", "") or ""),
    "static_url": str(getattr(settings, "STATIC_URL", "") or ""),
    "media_url": str(getattr(settings, "MEDIA_URL", "") or ""),
    "allowed_hosts": list(getattr(settings, "ALLOWED_HOSTS", [])),
    "database_engine": str(default_db.get("ENGINE", "") or ""),
    "database_name": str(default_db.get("NAME", "") or ""),
    "database_user": str(default_db.get("USER", "") or ""),
    "database_host": str(default_db.get("HOST", "") or ""),
    "database_port": str(default_db.get("PORT", "") or ""),
    "asgi_application": asgi_application,
    "has_channels": has_channels,
    "channel_layers": channels_layers,
    "is_asgi": bool(asgi_application) or has_channels,
}
print(json.dumps(payload))
"""

    result = run_command(
        [python_bin, "manage.py", "shell", "-c", script],
        cwd=project_root,
        timeout=180,
    )

    if not result["success"]:
        return build_result(
            False,
            "Django settings o'qib bo'lmadi",
            stderr=result["stderr"],
            stdout=result["stdout"],
            returncode=result["returncode"],
        )

    output = result["stdout"].strip().splitlines()
    if not output:
        return build_result(False, "Django settings JSON qaytmadi")

    try:
        settings_data = json.loads(output[-1])
    except json.JSONDecodeError as exc:
        return build_result(
            False,
            f"Django settings JSON parse bo'lmadi: {exc}",
            stdout=result["stdout"],
        )

    return build_result(True, "Django settings aniqlandi", settings=settings_data)


# settings.py oxiriga qo'shiladigan blok boshidagi belgi (takror qo'shmaslik uchun)
STATIC_MEDIA_MARKER = "# === Deploy robot tomonidan avtomatik qo'shildi ==="


def ensure_static_media_settings(project: dict, settings_data: dict):
    """
    settings.py da STATIC_ROOT / MEDIA_ROOT / STATIC_URL / MEDIA_URL bo'lmasa,
    ularni faylning oxiriga avtomatik qo'shadi. BASE_DIR bo'lsa undan, bo'lmasa
    project_root dan foydalanadi. Qaysi sozlamalar qo'shilganini qaytaradi.
    """
    settings_file = project.get("settings_file")
    if not settings_file or not os.path.exists(settings_file):
        return build_result(False, "settings.py topilmadi")

    try:
        text = Path(settings_file).read_text(encoding="utf-8", errors="ignore")
    except Exception as exc:
        return build_result(False, f"settings.py o'qib bo'lmadi: {exc}")

    if STATIC_MEDIA_MARKER in text:
        return build_result(True, "settings.py allaqachon to'ldirilgan", added=[])

    missing = {
        "STATIC_URL": not (settings_data.get("static_url") or ""),
        "STATIC_ROOT": not (settings_data.get("static_root") or ""),
        "MEDIA_URL": not (settings_data.get("media_url") or ""),
        "MEDIA_ROOT": not (settings_data.get("media_root") or ""),
    }

    if not any(missing.values()):
        return build_result(True, "STATIC/MEDIA sozlamalari mavjud", added=[])

    project_root = project.get("project_root") or "."

    lines = ["", STATIC_MEDIA_MARKER, "import os as _dr_os", "", "try:", "    _DR_BASE_DIR = str(BASE_DIR)", "except NameError:", f"    _DR_BASE_DIR = {project_root!r}", ""]

    added = []
    if missing["STATIC_URL"]:
        lines.append('STATIC_URL = "/static/"')
        added.append("STATIC_URL")
    if missing["STATIC_ROOT"]:
        lines.append('STATIC_ROOT = _dr_os.path.join(_DR_BASE_DIR, "staticfiles")')
        added.append("STATIC_ROOT")
    if missing["MEDIA_URL"]:
        lines.append('MEDIA_URL = "/media/"')
        added.append("MEDIA_URL")
    if missing["MEDIA_ROOT"]:
        lines.append('MEDIA_ROOT = _dr_os.path.join(_DR_BASE_DIR, "media")')
        added.append("MEDIA_ROOT")

    lines.append("")
    new_text = text.rstrip() + "\n" + "\n".join(lines)

    saved = save_text_file(settings_file, new_text)
    if not saved["success"]:
        return build_result(False, "settings.py yozib bo'lmadi")

    return build_result(True, "STATIC/MEDIA sozlamalari qo'shildi", added=added)


def ensure_directory(path: str):
    if not path:
        return build_result(True, "skip")

    os.makedirs(path, exist_ok=True)
    return build_result(True, f"created {path}")


def slugify_domain(domain: str):
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", domain.strip().lower())
    return slug.strip("-")


def service_name_for_domain(domain: str):
    return f"django-{slugify_domain(domain)}"


def socket_path_for_domain(domain: str):
    # RuntimeDirectory=<service> systemd tomonidan /run/<service> ni www-data
    # egaligida yaratadi, shuning uchun socket shu papka ichida bo'ladi
    return f"/run/{service_name_for_domain(domain)}/backend.sock"


def service_path_for_domain(domain: str):
    return f"/etc/systemd/system/{service_name_for_domain(domain)}.service"


def site_config_path_for_domain(domain: str):
    return f"/etc/nginx/sites-available/{domain}"


def site_enabled_path_for_domain(domain: str):
    return f"/etc/nginx/sites-enabled/{domain}"

def check_path(path):
    if not os.path.exists(path):
        return {"status": False, "msg": "❌ Path topilmadi"}

    if not os.path.exists(os.path.join(path, "package.json")):
        return {"status": False, "msg": "❌ package.json topilmadi"}

    return {"status": True, "msg": "✅ React loyiha aniqlandi"}


def npm_install(path):

    result = run_command(
        ["npm", "install"],
        cwd=path
    )

    return result

def build_project(path: str):

    return run_command(
        ["npm", "run", "build"],
        cwd=path
    )

def resolve_domain(domain: str):
    try:
        ip = socket.gethostbyname(domain)

        return {
            "status": True,
            "ip": ip
        }

    except socket.gaierror:

        return {
            "status": False,
            "msg": "Domain topilmadi"
        }
    
def check_dist(path: str):

    dist = os.path.join(path, "dist")

    if not os.path.exists(dist):

        return {
            "success": False,
            "msg": "dist papkasi topilmadi"
        }

    return {
        "success": True,
        "dist": dist
    }
    
def generate_nginx_config(domain: str, path: str):

    result = check_dist(path)

    if not result["success"]:
        return result

    template = os.path.join(
        os.path.dirname(__file__),
        "templates",
        "frontend.conf"
    )

    if not os.path.exists(template):

        return {
            "success": False,
            "msg": "frontend.conf topilmadi"
        }

    with open(template, "r") as f:
        config = f.read()

    config = config.replace(
        "{{DOMAIN}}",
        domain
    )

    config = config.replace(
        "{{DIST_PATH}}",
        result["dist"]
    )

    return {
        "success": True,
        "config": config
    }
    
def save_nginx_config(domain: str, config: str):

    os.makedirs(
        "/etc/nginx/sites-available",
        exist_ok=True
    )

    config_path = f"/etc/nginx/sites-available/{domain}"

    backup = None

    if os.path.exists(config_path):

        backup = config_path + ".bak"

        shutil.copy(
            config_path,
            backup
        )

    with open(config_path,"w",encoding="utf-8") as f:
        f.write(config)

    return {
        "success": True,
        "config_path": config_path,
        "backup": backup
    }
    
def enable_site(domain: str):

    available = f"/etc/nginx/sites-available/{domain}"
    enabled = f"/etc/nginx/sites-enabled/{domain}"

    os.makedirs(
        "/etc/nginx/sites-enabled",
        exist_ok=True
    )

    if os.path.islink(enabled):
        os.remove(enabled)

    if not os.path.exists(enabled):
        os.symlink(
            available,
            enabled
        )

    return {
        "success": True,
        "enabled": enabled
    }
    
def test_nginx():

    result = run_command(["nginx", "-t"])

    if not result["success"]:
        return {
            "success": False,
            "msg": result["stderr"]
        }

    return {
        "success": True,
        "msg": "Nginx konfiguratsiyasi tekshirildi"
    }
    
def reload_nginx():

    result = run_command(
        ["systemctl", "reload", "nginx"]
    )

    if not result["success"]:
        return {
            "success": False,
            "msg": result["stderr"]
        }

    return {
        "success": True,
        "msg": "Nginx qayta yuklandi"
    }
    
def check_site(domain: str):

    try:

        response = requests.get(
            f"http://{domain}",
            timeout=15,
            allow_redirects=True
        )

        return {
            "success": response.status_code in [200, 301, 302, 304],
            "status_code": response.status_code
        }

    except Exception as e:

        return {
            "success": False,
            "msg": str(e)
        }
        
def rollback(config_path=None, enabled_path=None, backup=None):
    """
    Deploy vaqtida xatolik bo'lsa nginx konfiguratsiyasini oldingi
    holatiga qaytaradi.
    """

    try:

        if enabled_path and os.path.islink(enabled_path):
            os.remove(enabled_path)

        if backup and os.path.exists(backup):
            shutil.copy(backup, config_path)

        elif config_path and os.path.exists(config_path):
            os.remove(config_path)

    except Exception as e:
        print("Rollback error:", e)
        
        
def deploy_frontend(path: str, domain: str):

    logs = []

    # 1. Config yaratish
    result = generate_nginx_config(domain, path)

    if not result["success"]:
        return result

    logs.append("✅ Nginx konfiguratsiyasi tayyorlandi")
    print("Config:", result["config"])

    # 2. Configni yozish
    result = save_nginx_config(
        domain,
        result["config"]
    )

    if not result["success"]:
        return result

    logs.append("✅ Konfiguratsiya saqlandi")
    print("Config path:", result["config_path"])

    config_path = result["config_path"]
    backup = result["backup"]

    # 3. Enable
    result = enable_site(domain)

    if not result["success"]:

        rollback(
            config_path=config_path,
            backup=backup
        )

        return result

    enabled_path = result["enabled"]

    logs.append("✅ Sayt faollashtirildi")
    print("Enabled path:", enabled_path)

    # 4. nginx test
    result = test_nginx()

    if not result["success"]:

        rollback(
            config_path=config_path,
            enabled_path=enabled_path,
            backup=backup
        )

        return {
            "success": False,
            "msg": result["stderr"]
        }

    logs.append("✅ Nginx konfiguratsiyasi tekshirildi")
    print("Test result:", result["msg"])

    # 5. nginx reload
    result = reload_nginx()

    if not result["success"]:

        rollback(
            config_path=config_path,
            enabled_path=enabled_path,
            backup=backup
        )

        return {
            "success": False,
            "msg": result["stderr"]
        }

    logs.append("✅ Nginx qayta yuklandi")
    print("Reload result:", result["msg"])

    # 6. Sayt tekshirish
    result = check_site(domain)

    if result["success"]:

        logs.append("✅ Sayt muvaffaqiyatli ishga tushdi")

        return {
            "success": True,
            "logs": logs,
            "url": f"http://{domain}"
        }

    logs.append("⚠️ Sayt ishga tushmadi")

    diag = diagnose_permissions(path)

    if not diag["success"]:

        logs.append("🔍 Permission muammosi aniqlandi")

        fix = fix_permissions(path)

        if fix["success"]:

            logs.append("🔧 Permission avtomatik tuzatildi")

            reload_nginx()

            result = check_site(domain)

            if result["success"]:

                logs.append("✅ Sayt qayta tekshiruvdan o'tdi")

                return {
                    "success": True,
                    "logs": logs,
                    "url": f"http://{domain}"
                }

    return {
        "success": False,
        "logs": logs,
        "msg": "Sayt ishga tushmadi"
    }
    
def diagnose_permissions(path: str):

    dist = os.path.join(path, "dist")

    result = run_command([
        "sudo",
        "-u",
        "www-data",
        "ls",
        dist
    ])

    if result["success"]:
        return {
            "success": True,
            "msg": "www-data dist papkasini o'qiy oladi"
        }

    return {
        "success": False,
        "msg": result["stderr"]
    }
    
def fix_permissions(path: str):

    dist = os.path.join(path, "dist")

    commands = [

        ["chmod", "755", "/home"],

        ["chmod", "755", "/home/smsplatform"],

        ["chmod", "755", path],

        ["find", dist, "-type", "d", "-exec", "chmod", "755", "{}", ";"],

        ["find", dist, "-type", "f", "-exec", "chmod", "644", "{}", ";"]

    ]

    logs = []

    for command in commands:

        result = run_command(command)

        if not result["success"]:

            return {
                "success": False,
                "msg": result["stderr"]
            }

        logs.append(" ".join(command))

    return {
        "success": True,
        "logs": logs
    }


def read_template(template_name: str):
    template_path = Path(__file__).parent / "templates" / template_name

    if not template_path.exists():
        return None

    return template_path.read_text(encoding="utf-8")


def save_text_file(file_path: str, content: str):
    backup = None

    if os.path.exists(file_path):
        backup = f"{file_path}.bak"
        shutil.copy(file_path, backup)

    os.makedirs(os.path.dirname(file_path), exist_ok=True)

    with open(file_path, "w", encoding="utf-8") as handle:
        handle.write(content)

    return {
        "success": True,
        "path": file_path,
        "backup": backup,
    }


def prepare_backend_project(path: str):
    project = resolve_django_project(path)

    if not project["status"]:
        return project

    project_root = project["project_root"]
    base_root = Path(project.get("base_root") or project_root)

    if not project.get("env_file"):
        return build_result(False, "❌ .env (yoki .env.example) topilmadi")

    if not os.path.exists(project["env_file"]):
        return build_result(False, "❌ .env topilmadi")

    # requirements.txt joylashgan papkada virtual muhit yaratib, kutubxonalarni
    # o'rnatamiz. Django check/migrate/collectstatic shu venv python bilan ishlaydi.
    venv_result = ensure_virtualenv(base_root)
    if not venv_result["status"]:
        return venv_result

    python_bin = venv_result["python_bin"]
    project["python_bin"] = python_bin
    project["gunicorn_bin"] = venv_result["gunicorn_bin"]
    project["venv_dir"] = venv_result["venv_dir"]
    venv_logs = venv_result.get("logs", [])
    if project.get("env_created"):
        venv_logs = venv_logs + [f"✅ .env fayli .env.example dan yaratildi: {project['env_file']}"]

    check_result = run_command(
        [python_bin, "manage.py", "check"],
        cwd=project_root,
        timeout=180,
    )

    if not check_result["success"]:
        return build_result(
            False,
            "❌ Django project check dan o'tmadi",
            stdout=check_result["stdout"],
            stderr=check_result["stderr"],
            returncode=check_result["returncode"],
        )

    settings_result = inspect_django_settings(project_root, python_bin)
    if not settings_result["status"]:
        return settings_result

    settings_data = settings_result["settings"]
    static_root = settings_data.get("static_root") or ""
    media_root = settings_data.get("media_root") or ""
    static_url = settings_data.get("static_url") or ""
    media_url = settings_data.get("media_url") or ""
    database_engine = (settings_data.get("database_engine") or "").lower()
    database_name = settings_data.get("database_name") or ""

    # STATIC_ROOT / MEDIA_ROOT / STATIC_URL / MEDIA_URL bo'lmasa — settings.py ga
    # avtomatik qo'shamiz va sozlamalarni qayta o'qiymiz.
    if not (static_root and media_root and static_url and media_url):
        fix = ensure_static_media_settings(project, settings_data)
        if fix["status"] and fix.get("added"):
            venv_logs = venv_logs + [
                f"✅ settings.py ga avtomatik qo'shildi: {', '.join(fix['added'])}"
            ]
            settings_result = inspect_django_settings(project_root, python_bin)
            if settings_result["status"]:
                settings_data = settings_result["settings"]
                static_root = settings_data.get("static_root") or ""
                media_root = settings_data.get("media_root") or ""
                static_url = settings_data.get("static_url") or ""
                media_url = settings_data.get("media_url") or ""

    if not static_root:
        return build_result(False, "❌ settings.py da STATIC_ROOT topilmadi va qo'shib bo'lmadi")

    if not media_root:
        return build_result(False, "❌ settings.py da MEDIA_ROOT topilmadi va qo'shib bo'lmadi")

    if not static_url:
        return build_result(False, "❌ settings.py da STATIC_URL topilmadi va qo'shib bo'lmadi")

    if not media_url:
        return build_result(False, "❌ settings.py da MEDIA_URL topilmadi va qo'shib bo'lmadi")

    if not database_engine:
        return build_result(False, "❌ settings.py da database engine topilmadi")

    # Websocket / ASGI aniqlash: settings dagi ASGI_APPLICATION yoki channels/daphne
    # bo'lsa hamda asgi.py fayli mavjud bo'lsa, ASGI rejimida deploy qilamiz.
    is_asgi = bool(settings_data.get("is_asgi")) and bool(project.get("asgi_file"))

    checks = [
        f"manage.py: {project['manage_py']}",
        f"settings.py: {project['settings_file']}",
        f".env: {project['env_file']}",
        f"STATIC_ROOT: {static_root}",
        f"MEDIA_ROOT: {media_root}",
        f"STATIC_URL: {static_url}",
        f"MEDIA_URL: {media_url}",
        f"Database engine: {database_engine}",
        f"Rejim: {'ASGI/websocket (' + project.get('asgi_module', '') + ')' if is_asgi else 'WSGI (' + project['wsgi_module'] + ')'}",
    ]

    if is_asgi:
        checks.append("🔌 Websocket (ASGI) aniqlandi — uvicorn worker va nginx upgrade sozlamalari ishlatiladi")

    sqlite_path = ""
    if "sqlite" in database_engine:
        sqlite_path = database_name
        if sqlite_path and not os.path.isabs(sqlite_path):
            sqlite_path = str((Path(project_root) / sqlite_path).resolve())

        if sqlite_path and os.path.exists(sqlite_path):
            checks.append(f"SQLite database: {sqlite_path}")
        else:
            checks.append("SQLite database hozircha yo'q, migrate uni yaratadi")
    else:
        checks.append("PostgreSQL yoki boshqa external database topildi")

    return build_result(
        True,
        "Django REST API tayyor",
        project=project,
        settings=settings_data,
        checks=checks,
        sqlite_path=sqlite_path,
        is_asgi=is_asgi,
        venv_logs=venv_logs,
    )


def check_backend_path(path: str):
    result = prepare_backend_project(path)

    if not result["status"]:
        return result

    return build_result(
        True,
        "✅ Django REST API aniqlandi",
        checks=result.get("checks", []),
        project=result.get("project", {}),
        settings=result.get("settings", {}),
    )


# Websocket (ASGI) rejimida location / ichiga qo'shiladigan upgrade sozlamalari
WEBSOCKET_PROXY_BLOCK = (
    "        proxy_http_version 1.1;\n"
    "        proxy_set_header Upgrade $http_upgrade;\n"
    "        proxy_set_header Connection $connection_upgrade;\n"
    "        proxy_read_timeout 86400;"
)


def ensure_websocket_map():
    """
    $connection_upgrade map ni http kontekstiga bir marta yozadi. Har bir sayt
    configida takrorlanmasligi uchun alohida global faylda saqlanadi.
    """
    map_config = (
        "map $http_upgrade $connection_upgrade {\n"
        "    default upgrade;\n"
        "    ''      close;\n"
        "}\n"
    )
    return save_text_file("/etc/nginx/conf.d/websocket_upgrade.conf", map_config)


def build_static_location(url: str, root: str):
    """
    static/media uchun nginx `location` bloki quradi. Agar url `/`, bo'sh yoki
    tashqi URL (CDN) bo'lsa — asosiy `location /` bilan to'qnashmasligi uchun
    hech narsa qaytarmaydi (aks holda 'duplicate location /' xatosi chiqadi).
    """
    url = (url or "").strip()
    root = (root or "").strip().rstrip("/")

    if not url or not root:
        return ""

    if not url.startswith("/"):
        url = "/" + url

    if url == "/" or "://" in url:
        return ""

    return (
        f"    location {url} {{\n"
        f"        alias {root}/;\n"
        f"        access_log off;\n"
        f"        expires 30d;\n"
        f"    }}\n\n"
    )


def generate_backend_nginx_config(domain: str, settings_data: dict, socket_path: str, is_asgi: bool = False):
    template = read_template("backend.conf")

    if template is None:
        return build_result(False, "backend.conf topilmadi")

    static_root = settings_data.get("static_root") or ""
    media_root = settings_data.get("media_root") or ""
    static_url = settings_data.get("static_url") or ""
    media_url = settings_data.get("media_url") or ""
    upstream_name = service_name_for_domain(domain)
    websocket_proxy = WEBSOCKET_PROXY_BLOCK if is_asgi else ""

    static_location = build_static_location(static_url, static_root)
    media_location = build_static_location(media_url, media_root)

    config = template.replace("{{DOMAIN}}", domain)
    config = config.replace("{{STATIC_LOCATION}}", static_location)
    config = config.replace("{{MEDIA_LOCATION}}", media_location)
    config = config.replace("{{UPSTREAM_NAME}}", upstream_name)
    config = config.replace("{{SOCKET_PATH}}", socket_path)
    config = config.replace("{{WEBSOCKET_PROXY}}", websocket_proxy)

    return build_result(True, "Nginx config tayyor", config=config)


def build_exec_start(project: dict, socket_path: str, is_asgi: bool):
    gunicorn_bin = project.get("gunicorn_bin") or "gunicorn"
    project_root = project.get("project_root") or ""

    if is_asgi and project.get("asgi_module"):
        module = project["asgi_module"]
        return (
            f"{gunicorn_bin} --chdir {project_root} "
            f"--workers 3 --worker-class uvicorn.workers.UvicornWorker "
            f"--bind unix:{socket_path} {module}:application"
        )

    module = project.get("wsgi_module") or ""
    return (
        f"{gunicorn_bin} --chdir {project_root} "
        f"--workers 3 --bind unix:{socket_path} {module}:application"
    )


def generate_backend_service_config(domain: str, project: dict, socket_path: str, is_asgi: bool = False):
    template = read_template("backend.service")

    if template is None:
        return build_result(False, "backend.service topilmadi")

    env_file = project.get("env_file") or ""
    project_root = project.get("project_root") or ""
    service_name = service_name_for_domain(domain)
    env_file_line = f"EnvironmentFile={env_file}" if env_file else ""
    exec_start = build_exec_start(project, socket_path, is_asgi)

    config = template.replace("{{DOMAIN}}", domain)
    config = config.replace("{{SERVICE_NAME}}", service_name)
    config = config.replace("{{PROJECT_ROOT}}", project_root)
    config = config.replace("{{ENV_FILE_LINE}}", env_file_line)
    config = config.replace("{{EXEC_START}}", exec_start)
    config = config.replace("{{SOCKET_PATH}}", socket_path)

    return build_result(True, "Systemd service tayyor", config=config)


def save_backend_service(service_name: str, config: str):
    service_path = f"/etc/systemd/system/{service_name}.service"
    return save_text_file(service_path, config)


def systemd_daemon_reload():
    result = run_command(["systemctl", "daemon-reload"])

    if not result["success"]:
        return build_result(False, result["stderr"], stdout=result["stdout"])

    return build_result(True, "systemd qayta yuklandi")


def enable_and_start_service(service_name: str):
    result = run_command(["systemctl", "enable", "--now", service_name], timeout=180)

    if not result["success"]:
        return build_result(
            False,
            f"Service ishga tushmadi: {service_name}",
            stdout=result["stdout"],
            stderr=result["stderr"],
            returncode=result["returncode"],
        )

    return build_result(True, f"Service ishga tushdi: {service_name}")


def service_is_active(service_name: str):
    result = run_command(["systemctl", "is-active", service_name], timeout=30)
    return result["success"]


def service_status(service_name: str):
    return run_command(["systemctl", "status", service_name, "--no-pager", "-l"], timeout=60)


def run_manage_command(project_root: str, python_bin: str, args: list):
    return run_command([python_bin, "manage.py", *args], cwd=project_root, timeout=1800)


def collect_static(project_root: str, python_bin: str):
    return run_manage_command(project_root, python_bin, ["collectstatic", "--noinput"])


def migrate_database(project_root: str, python_bin: str):
    return run_manage_command(project_root, python_bin, ["migrate", "--noinput"])


def diagnose_backend_permissions(path: str, static_root: str, media_root: str):
    checks = [
        path,
        static_root,
        media_root,
    ]

    for item in checks:
        if not item:
            continue

        result = run_command(
            ["sudo", "-u", "www-data", "ls", item],
            timeout=30,
        )

        if not result["success"]:
            return build_result(
                False,
                f"www-data '{item}' pathini o'qiy olmayapti",
                stderr=result["stderr"],
                stdout=result["stdout"],
            )

    return build_result(True, "www-data pathlarni o'qiy oladi")


def fix_backend_permissions(path: str, static_root: str, media_root: str):
    commands = [
        ["chown", "-R", "www-data:www-data", path],
    ]

    for extra_path in [static_root, media_root]:
        if extra_path and extra_path != path:
            commands.append(["chown", "-R", "www-data:www-data", extra_path])

    commands.extend(
        [
            ["find", path, "-type", "d", "-exec", "chmod", "755", "{}", ";"],
            ["find", path, "-type", "f", "-name", "*.py", "-exec", "chmod", "644", "{}", ";"],
            ["find", path, "-type", "f", "-name", "*.sqlite3", "-exec", "chmod", "664", "{}", ";"],
            ["find", path, "-type", "f", "-name", ".env", "-exec", "chmod", "640", "{}", ";"],
        ]
    )

    for extra_path in [static_root, media_root]:
        if extra_path:
            commands.extend(
                [
                    ["find", extra_path, "-type", "d", "-exec", "chmod", "755", "{}", ";"],
                    ["find", extra_path, "-type", "f", "-exec", "chmod", "644", "{}", ";"],
                ]
            )

    logs = []

    for command in commands:
        result = run_command(command)
        if not result["success"]:
            return build_result(False, result["stderr"], logs=logs)

        logs.append(" ".join(command))

    return build_result(True, "Permissions fix qilindi", logs=logs)


def check_site_status(domain: str):
    try:
        response = requests.get(
            f"http://{domain}",
            timeout=20,
            allow_redirects=True,
        )

        return {
            "success": response.status_code in [200, 301, 302, 304],
            "status_code": response.status_code,
            "text": response.text[:1000],
        }
    except Exception as exc:
        return {
            "success": False,
            "msg": str(exc),
        }


def ensure_certbot():
    if shutil.which("certbot"):
        return build_result(True, "certbot mavjud")

    run_command(["apt-get", "update"], timeout=300)
    install = run_command(
        ["apt-get", "install", "-y", "certbot", "python3-certbot-nginx"],
        timeout=900,
    )

    if shutil.which("certbot"):
        return build_result(True, "certbot o'rnatildi")

    return build_result(
        False,
        "certbot o'rnatilmadi",
        stderr=install["stderr"],
        stdout=install["stdout"],
    )


def obtain_ssl_certificate(domain: str):
    """
    Domainga Let's Encrypt SSL sertifikat oladi va nginx ni HTTPS ga o'tkazadi.
    HTTP dan HTTPS ga avtomatik redirect qo'shiladi. Xatolik bo'lsa deploy
    to'xtamaydi — sayt HTTP da ishlayveradi.
    """
    ready = ensure_certbot()
    if not ready["status"]:
        return ready

    args = [
        "certbot", "--nginx",
        "-d", domain,
        "--non-interactive",
        "--agree-tos",
        "--redirect",
    ]

    # www subdomeni serverga ulangan bo'lsagina sertifikatga qo'shamiz
    www_domain = f"www.{domain}"
    if resolve_domain(www_domain).get("status"):
        args += ["-d", www_domain]

    if LETSENCRYPT_EMAIL:
        args += ["-m", LETSENCRYPT_EMAIL]
    else:
        args += ["--register-unsafely-without-email"]

    result = run_command(args, timeout=300)
    if not result["success"]:
        return build_result(
            False,
            "SSL sertifikat olinmadi",
            stderr=result["stderr"],
            stdout=result["stdout"],
        )

    return build_result(True, "SSL sertifikat o'rnatildi va HTTPS yoqildi")


def apply_ssl_and_finalize(domain: str, logs: list, **extra):
    """Muvaffaqiyatli deploy so'ngida SSL o'rnatib, yakuniy natijani qaytaradi."""
    ssl_result = obtain_ssl_certificate(domain)
    if ssl_result["status"]:
        logs.append("🔒 SSL sertifikat o'rnatildi (HTTPS)")
        url = f"https://{domain}"
    else:
        logs.append(f"⚠️ SSL o'rnatilmadi: {ssl_result['msg']} (sayt HTTP da ishlayapti)")
        url = f"http://{domain}"

    return build_result(
        True,
        "Django backend deploy yakunlandi",
        logs=logs,
        url=url,
        ssl=ssl_result["status"],
        **extra,
    )


def deploy_backend(path: str, domain: str):
    logs = []
    domain = (domain or "").strip()

    if not domain:
        return build_result(False, "Domain kiritilmadi")

    prepared = prepare_backend_project(path)
    if not prepared["status"]:
        return prepared

    project = prepared["project"]
    settings_data = prepared["settings"]
    is_asgi = prepared.get("is_asgi", False)
    project_root = project["project_root"]
    python_bin = project["python_bin"]
    static_root = settings_data["static_root"]
    media_root = settings_data["media_root"]
    service_name = service_name_for_domain(domain)
    socket_path = socket_path_for_domain(domain)
    site_config_path = site_config_path_for_domain(domain)
    site_enabled_path = site_enabled_path_for_domain(domain)
    service_file_path = service_path_for_domain(domain)

    if service_is_active(service_name) and os.path.islink(site_enabled_path):
        return build_result(
            True,
            f"Bu domain uchun service allaqachon ishlayapti: {domain}",
            already_running=True,
            service_name=service_name,
            url=f"http://{domain}",
        )

    ensure_directory(static_root)
    ensure_directory(media_root)

    # Virtual muhit va .env tayyorlash loglari
    logs.extend(prepared.get("venv_logs", []))
    logs.append("✅ Django project tekshiruvdan o'tdi")

    migrate_result = migrate_database(project_root, python_bin)
    if not migrate_result["success"]:
        return build_result(
            False,
            "❌ migrate bajarilmadi",
            logs=logs,
            stdout=migrate_result["stdout"],
            stderr=migrate_result["stderr"],
            returncode=migrate_result["returncode"],
        )

    logs.append("✅ migrate bajarildi")

    collect_result = collect_static(project_root, python_bin)
    if not collect_result["success"]:
        return build_result(
            False,
            "❌ collectstatic bajarilmadi",
            logs=logs,
            stdout=collect_result["stdout"],
            stderr=collect_result["stderr"],
            returncode=collect_result["returncode"],
        )

    logs.append("✅ collectstatic bajarildi")

    # migrate/collectstatic root sifatida ishlaydi, shuning uchun kod, venv,
    # static/media va DB fayllarini www-data egaligiga o'tkazamiz
    fix_result = fix_backend_permissions(project_root, static_root, media_root)
    if fix_result["status"]:
        logs.append("✅ Fayl huquqlari www-data uchun sozlandi")
    else:
        logs.append(f"⚠️ Permission sozlashda muammo: {fix_result['msg']}")

    if is_asgi:
        ensure_websocket_map()
        logs.append("🔌 Websocket (ASGI) rejimi: nginx upgrade map yozildi")

    nginx_result = generate_backend_nginx_config(domain, settings_data, socket_path, is_asgi)
    if not nginx_result["success"]:
        return build_result(False, nginx_result["msg"], logs=logs)

    service_result = generate_backend_service_config(domain, project, socket_path, is_asgi)
    if not service_result["success"]:
        return build_result(False, service_result["msg"], logs=logs)

    config_result = save_text_file(site_config_path, nginx_result["config"])
    if not config_result["success"]:
        return build_result(False, "Nginx config saqlanmadi", logs=logs)

    logs.append("✅ Nginx konfiguratsiyasi saqlandi")

    service_save_result = save_backend_service(service_name, service_result["config"])
    if not service_save_result["success"]:
        return build_result(False, "Systemd service saqlanmadi", logs=logs)

    logs.append("✅ Systemd service saqlandi")

    enable_result = enable_site(domain)
    if not enable_result["success"]:
        rollback(
            config_path=site_config_path,
            enabled_path=site_enabled_path,
            backup=config_result["backup"],
        )
        return build_result(False, enable_result.get("msg", "Nginx site enable bo'lmadi"), logs=logs)

    logs.append("✅ Nginx site yoqildi")

    daemon_result = systemd_daemon_reload()
    if not daemon_result["success"]:
        return build_result(False, daemon_result["msg"], logs=logs)

    logs.append("✅ systemd daemon-reload bajarildi")

    start_result = enable_and_start_service(service_name)
    if not start_result["success"]:
        status_result = service_status(service_name)
        rollback(
            config_path=site_config_path,
            enabled_path=site_enabled_path,
            backup=config_result["backup"],
        )
        return build_result(
            False,
            start_result["msg"],
            logs=logs,
            stdout=status_result["stdout"],
            stderr=status_result["stderr"],
        )

    logs.append("✅ Service ishga tushdi")

    nginx_test = test_nginx()
    if not nginx_test["success"]:
        rollback(
            config_path=site_config_path,
            enabled_path=site_enabled_path,
            backup=config_result["backup"],
        )
        return build_result(False, nginx_test["msg"], logs=logs)

    logs.append("✅ Nginx konfiguratsiyasi tekshirildi")

    reload_result = reload_nginx()
    if not reload_result["success"]:
        rollback(
            config_path=site_config_path,
            enabled_path=site_enabled_path,
            backup=config_result["backup"],
        )
        return build_result(False, reload_result["msg"], logs=logs)

    logs.append("✅ Nginx qayta yuklandi")

    site_result = check_site_status(domain)
    if site_result.get("success"):
        logs.append("✅ Django backend muvaffaqiyatli ishga tushdi")
        return apply_ssl_and_finalize(
            domain,
            logs,
            service_name=service_name,
            site_config=site_config_path,
            service_file=service_file_path,
        )

    logs.append(f"⚠️ Sayt javobi muammo berdi: {site_result.get('status_code') or site_result.get('msg')}")

    permission_check = diagnose_backend_permissions(project_root, static_root, media_root)
    if not permission_check["status"]:
        logs.append("🔍 Permission muammosi topildi, qayta tuzatilmoqda")
        fix_result = fix_backend_permissions(project_root, static_root, media_root)
        if fix_result["status"]:
            logs.extend(fix_result.get("logs", []))
            restart_result = run_command(["systemctl", "restart", service_name], timeout=180)
            if restart_result["success"]:
                logs.append("✅ Service qayta ishga tushdi")
                reload_nginx()
                site_result = check_site_status(domain)
                if site_result.get("success"):
                    logs.append("✅ Sayt qayta tekshiruvdan o'tdi")
                    return apply_ssl_and_finalize(
                        domain,
                        logs,
                        service_name=service_name,
                    )

        else:
            logs.append(f"⚠️ Permission fix muvaffaqiyatsiz: {fix_result['msg']}")

    return build_result(
        False,
        "Backend sayti ishga tushmadi",
        logs=logs,
        status_code=site_result.get("status_code"),
        site_error=site_result.get("msg") or site_result.get("text"),
        service_name=service_name,
    )
