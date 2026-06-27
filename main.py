from fastapi import FastAPI
from executor import (
    check_path,
    npm_install,
    build_project,
    resolve_domain,
    deploy_frontend
)
import requests

app = FastAPI()

# 🔗 BACKEND URL
BACKEND_URL = "https://coordinate-gone-bull-sao.trycloudflare.com"

# 🚀 REGISTER FUNKSIYA
def register():
    try:
        requests.post(f"{BACKEND_URL}/servers", json={
            "name": "ubuntu server",
            "ip": "151.247.208.29:9000"
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