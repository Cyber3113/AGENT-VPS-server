import os
import shutil
import socket
import subprocess
import requests

from utils import run_command

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