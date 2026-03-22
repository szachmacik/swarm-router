"""
HOLON AGENT SWARM ROUTER — FastAPI on Coolify
Same API as planned CF Worker, but runs on DO server.
Upgrades to CF Workers when API token available.

Zasada pomocniczości: Ollama → Haiku → OpenManus → spawn new
Cost: logarithmically → $0
"""
import os, json, asyncio, time, logging, httpx
from fastapi import FastAPI, BackgroundTasks, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime, timezone
from supabase import create_client

log = logging.getLogger("swarm-router")
logging.basicConfig(level=logging.INFO, format='%(asctime)s [ROUTER] %(message)s')

SWARM_SECRET  = os.environ.get("SWARM_SECRET", "holon-swarm-2026")
COOLIFY_TOKEN = os.environ.get("COOLIFY_TOKEN", "")
COOLIFY_URL   = os.environ.get("COOLIFY_URL", "https://coolify.ofshore.dev")
SUPABASE_URL  = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY  = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
GH_TOKEN      = os.environ.get("GH_TOKEN", "")
PORT          = int(os.environ.get("PORT", "3000"))

supabase = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None

app = FastAPI(title="Holon Agent Swarm Router", version="1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# In-memory agent registry (backed by Supabase)
agent_registry: dict = {}

def auth(secret: str) -> bool:
    return secret == SWARM_SECRET

async def get_agents() -> list:
    if not supabase:
        return []
    try:
        result = supabase.table("agent_pool").select("*").neq("status", "dead").order("current_tasks").execute()
        return result.data or []
    except:
        return []

async def update_agent(agent_id: str, updates: dict):
    if not supabase:
        return
    try:
        supabase.table("agent_pool").update(updates).eq("id", agent_id).execute()
    except Exception as e:
        log.warning(f"Agent update error: {e}")

async def log_task(task_id: str, agent_id: str, task_type: str, status: str):
    if not supabase:
        return
    try:
        supabase.table("agent_tasks").upsert({
            "id": task_id, "agent_id": agent_id,
            "task_type": task_type, "status": status,
            "started_at": datetime.now(timezone.utc).isoformat()
        }).execute()
    except: pass

async def select_best_agent(agents: list, complexity: str) -> dict | None:
    """Subsidiarity: pick cheapest capable agent."""
    # Priority: mini (free) → haiku ($0.0002) → openmanus ($0.002)
    type_priority = {"mini": 0, "haiku": 1, "openmanus": 2}
    if complexity == "complex":
        type_priority = {"openmanus": 0, "haiku": 1, "mini": 2}
    elif complexity == "medium":
        type_priority = {"haiku": 0, "mini": 1, "openmanus": 2}
    
    available = [a for a in agents if a["current_tasks"] < a["max_tasks"]]
    if not available:
        return None
    
    available.sort(key=lambda a: (type_priority.get(a["type"], 9), a["current_tasks"]))
    return available[0] if available else None

async def spawn_agent(background_tasks: BackgroundTasks):
    """Auto-spawn new mini-agent via Coolify API."""
    log.info("Spawning new mini-agent (Kairos)...")
    
    # Check cooldown
    if supabase:
        cfg = supabase.table("swarm_config").select("value").eq("key", "last_spawn").execute()
        if cfg.data:
            last = int(cfg.data[0]["value"] or "0")
            if time.time() - last < 120:
                log.info("Spawn cooldown active")
                return

    async def do_spawn():
        try:
            async with httpx.AsyncClient(timeout=30) as http:
                # Create project
                p = await http.post(f"{COOLIFY_URL}/api/v1/projects",
                    headers={"Authorization": f"Bearer {COOLIFY_TOKEN}", "Content-Type": "application/json"},
                    json={"name": f"mini-agent-{int(time.time())}", "description": "Auto-spawned swarm worker"})
                proj = p.json()
                proj_uuid = proj.get("uuid")
                if not proj_uuid:
                    return

                pd = await http.get(f"{COOLIFY_URL}/api/v1/projects/{proj_uuid}",
                    headers={"Authorization": f"Bearer {COOLIFY_TOKEN}"})
                env_uuid = pd.json().get("environments", [{}])[0].get("uuid")

                a = await http.post(f"{COOLIFY_URL}/api/v1/applications/public",
                    headers={"Authorization": f"Bearer {COOLIFY_TOKEN}", "Content-Type": "application/json"},
                    json={
                        "project_uuid": proj_uuid,
                        "environment_uuid": env_uuid,
                        "server_uuid": "iswgwwcccc408o8kgkccccss",
                        "git_repository": f"https://{GH_TOKEN}@github.com/szachmacik/mini-agent",
                        "git_branch": "main",
                        "build_pack": "dockerfile",
                        "name": f"mini-{int(time.time())}",
                        "ports_exposes": "3000"
                    })
                new_app = a.json()
                app_uuid = new_app.get("uuid")
                if not app_uuid:
                    return

                # Set envs
                envs = [
                    ("AGENT_ID", f"auto-mini-{app_uuid[:8]}"),
                    ("ANTHROPIC_API_KEY", os.environ.get("ANTHROPIC_API_KEY", "")),
                    ("SUPABASE_URL", SUPABASE_URL),
                    ("SUPABASE_SERVICE_ROLE_KEY", SUPABASE_KEY),
                    ("COOLIFY_TOKEN", COOLIFY_TOKEN),
                    ("COOLIFY_URL", COOLIFY_URL),
                    ("OLLAMA_URL", "http://ollama-ollama-1:11434"),
                    ("OLLAMA_MODEL", "qwen2.5:0.5b"),
                    ("SWARM_SECRET", SWARM_SECRET),
                    ("SWARM_ROUTER_URL", f"http://swarm-router.ofshore.dev"),
                    ("PORT", "3000"),
                ]
                for k, v in envs:
                    await http.post(f"{COOLIFY_URL}/api/v1/applications/{app_uuid}/envs",
                        headers={"Authorization": f"Bearer {COOLIFY_TOKEN}", "Content-Type": "application/json"},
                        json={"key": k, "value": v, "is_preview": False})

                await http.get(f"{COOLIFY_URL}/api/v1/applications/{app_uuid}/restart",
                    headers={"Authorization": f"Bearer {COOLIFY_TOKEN}"})

                # Register in Supabase
                if supabase:
                    supabase.table("agent_pool").insert({
                        "id": f"auto-{app_uuid[:12]}",
                        "name": f"Auto Mini Agent {app_uuid[:8]}",
                        "type": "mini",
                        "endpoint": f"http://{app_uuid}.178.62.246.169.sslip.io",
                        "status": "starting",
                        "max_tasks": 10,
                        "cost_per_task": 0.0,
                        "spawned_by": "swarm-router-auto"
                    }).execute()
                    supabase.table("swarm_config").upsert({"key": "last_spawn", "value": str(int(time.time()))}).execute()

                log.info(f"✅ Spawned agent: {app_uuid}")
        except Exception as e:
            log.error(f"Spawn failed: {e}")

    asyncio.create_task(do_spawn())

# ── Routes ─────────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "service": "swarm-router", "ts": datetime.now(timezone.utc).isoformat()}

@app.get("/agents")
async def agents():
    data = await get_agents()
    return {"agents": data, "count": len(data)}

@app.get("/metrics")
async def metrics():
    agents_data = await get_agents()
    idle = sum(1 for a in agents_data if a["status"] == "idle")
    busy = sum(1 for a in agents_data if a["status"] == "busy")
    try:
        tasks = supabase.table("agent_tasks").select("status").execute() if supabase else None
        task_counts = {}
        for t in (tasks.data or []):
            task_counts[t["status"]] = task_counts.get(t["status"], 0) + 1
    except:
        task_counts = {}
    
    done = task_counts.get("done", 0)
    failed = task_counts.get("failed", 0)
    total = done + failed
    prime = round(done / max(1, total), 4)
    
    return {
        "prime_score": prime,
        "agents": len(agents_data),
        "agents_idle": idle, "agents_busy": busy,
        "tasks": task_counts,
        "subsidiarity": "ollama→haiku→openmanus"
    }

@app.post("/task")
async def submit_task(request: Request, background_tasks: BackgroundTasks,
                       x_swarm_secret: str = Header(None)):
    if not auth(x_swarm_secret or ""):
        raise HTTPException(401, "Unauthorized")
    
    body = await request.json()
    task_type = body.get("task_type", "general")
    payload = body.get("payload", {})
    complexity = body.get("complexity", "simple")
    
    import uuid
    task_id = str(uuid.uuid4())
    
    agents = await get_agents()
    agent = await select_best_agent(agents, complexity)
    
    if not agent:
        # Spawn new and queue
        await spawn_agent(background_tasks)
        if supabase:
            supabase.table("agent_tasks").insert({
                "id": task_id, "task_type": task_type,
                "payload": json.dumps(payload), "status": "queued"
            }).execute()
        return {"task_id": task_id, "status": "queued", "message": "Spawning new agent (Kairos)", "wait_s": 120}
    
    # Assign and forward
    await update_agent(agent["id"], {"current_tasks": agent["current_tasks"] + 1, "status": "busy"})
    await log_task(task_id, agent["id"], task_type, "dispatched")
    
    async def forward_and_complete():
        try:
            async with httpx.AsyncClient(timeout=60) as http:
                r = await http.post(f"{agent['endpoint']}/execute",
                    headers={"x-swarm-secret": SWARM_SECRET, "Content-Type": "application/json"},
                    json={"task_id": task_id, "task_type": task_type, "payload": payload, "complexity": complexity})
                status = "done" if r.status_code == 200 else "failed"
        except Exception as e:
            log.error(f"Forward error: {e}")
            status = "failed"
        
        await update_agent(agent["id"], {"current_tasks": max(0, agent["current_tasks"])})
        if supabase:
            supabase.table("agent_tasks").update({"status": status, "finished_at": datetime.now(timezone.utc).isoformat()}).eq("id", task_id).execute()
    
    background_tasks.add_task(forward_and_complete)
    return {"task_id": task_id, "status": "dispatched", "agent": agent["name"], "type": agent["type"]}

@app.post("/agent/heartbeat")
async def heartbeat(request: Request, x_swarm_secret: str = Header(None)):
    if not auth(x_swarm_secret or ""):
        raise HTTPException(401)
    body = await request.json()
    await update_agent(body["agent_id"], {
        "status": body.get("status", "idle"),
        "current_tasks": body.get("current_tasks", 0),
        "avg_ms": body.get("avg_ms", 0),
        "success_rate": body.get("success_rate", 1.0),
        "last_heartbeat": datetime.now(timezone.utc).isoformat()
    })
    return {"ok": True}

@app.post("/spawn")
async def spawn_endpoint(request: Request, background_tasks: BackgroundTasks, x_swarm_secret: str = Header(None)):
    if not auth(x_swarm_secret or ""):
        raise HTTPException(401)
    await spawn_agent(background_tasks)
    return {"spawning": True}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
