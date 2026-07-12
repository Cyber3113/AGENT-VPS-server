from fastapi import FastAPI
from executor import (
    check_path,
    check_backend_path,
    npm_install,
    build_project,
    resolve_domain,
    deploy_frontend,
    deploy_backend,
)
import requests

app = FastAPI()

# 🔗 BACKEND URL
BACKEND_URL = "https://pre-hygiene-guy-dock.trycloudflare.com"

# 🚀 REGISTER FUNKSIYA
def register():
    try:
        requests.post(f"{BACKEND_URL}/servers", json={
            "name": "foodly server",
            "ip": "82.25.185.204:9000"
        })
        print("✅ Registered to backend")
    except Exception as e:
        print("❌ Register error:", e)


# 🔥 STARTDA ISHLAYDI
@app.on_event("startup")
def startup_event():
    register()


# 📌 API endpoint
@app.post("/check-path")
def check(data: dict):
    path = data.get("path")
    return check_path(path)

@app.post("/check-backend-path")
def check_backend(data: dict):
    path = data.get("path")
    return check_backend_path(path)

@app.post("/npm-install")
def install(data: dict):
    path = data.get("path")
    return npm_install(path)    

@app.post("/npm-build")
def build(data: dict):
    path = data.get("path")
    return build_project(path)    

@app.post("/check-domain")
def check_domain_api(data: dict):
    domain = data.get("domain")
    return resolve_domain(domain)

@app.post("/deploy-frontend")
def deploy_frontend_api(data: dict):

    return deploy_frontend(
        data["path"],
        data["domain"]
    )

@app.post("/deploy-backend")
def deploy_backend_api(data: dict):
    return deploy_backend(
        data["path"],
        data["domain"]
    )
