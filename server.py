from __future__ import annotations
import asyncio, calendar, gzip, hmac, json, os, time
from datetime import date, datetime
from zoneinfo import ZoneInfo
from typing import Any
import httpx
from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

TRAIN = "https://trains.xunjiapp.cn"
FOOD = "https://eatings.xunjiapp.cn"
API = "https://api.xunjiapp.cn"
TZ = ZoneInfo(os.getenv("APP_TIMEZONE", "Asia/Hong_Kong"))
ACCESS = os.getenv("MCP_ACCESS_TOKEN", "")
TRAIN_KEY = os.getenv("XUNJI_TRAIN_API_KEY", "")
FOOD_KEY = os.getenv("XUNJI_FOOD_API_KEY", "")
BODY_KEY = os.getenv("XUNJI_BODY_API_KEY", "")
mcp = MCPServer("xunji-read-only-bridge")
client = httpx.AsyncClient(timeout=60.0)
cache: dict[str, tuple[float, Any]] = {}
last: dict[str, float] = {}
locks: dict[str, asyncio.Lock] = {}

def day(s: str) -> date:
    try:
        d = date.fromisoformat(s)
    except ValueError as e:
        raise ValueError("日期须为 YYYY-MM-DD") from e
    if d.isoformat() != s: raise ValueError("日期须为 YYYY-MM-DD")
    return d

def months(d: date, n: int) -> date:
    m = d.month - 1 + n
    y, m = d.year + m // 12, m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))

def date_range(a: str, b: str, food=False, max_days=None):
    x, y = day(a), day(b)
    if y < x: raise ValueError("end_date 不可早于 start_date")
    if max_days and (y-x).days + 1 > max_days: raise ValueError(f"日期范围不可超过 {max_days} 天")
    if food:
        today = datetime.now(TZ).date()
        if x < months(today, -12) or y > months(today, 3):
            raise ValueError("饮食查询范围限过去一年至未来三个月")

async def query(url: str, key: str, payload: dict, cache_id: str, limit_id: str, cooldown=15):
    if not key: raise RuntimeError("Render 尚未配置对应的训记 API Key")
    now = time.monotonic()
    hit = cache.get(cache_id)
    if hit and hit[0] > now: return hit[1]
    lock = locks.setdefault(limit_id, asyncio.Lock())
    async with lock:
        now = time.monotonic()
        hit = cache.get(cache_id)
        if hit and hit[0] > now: return hit[1]
        prev = last.get(limit_id)
        if prev is not None and now-prev < cooldown:
            raise RuntimeError(f"训记接口限频，请约 {int(cooldown-(now-prev))+1} 秒后重试")
        last[limit_id] = now
        try:
            r = await client.post(url, headers={
                "Authorization": f"Bearer {key}",
                "Accept-Encoding": "gzip",
                "Content-Type": "application/json",
            }, json=payload)
        except httpx.HTTPError as e:
            raise RuntimeError("连接训记接口失败或超时") from e
        if r.status_code == 429: raise RuntimeError("训记接口限频，请稍后重试")
        if r.status_code >= 400: raise RuntimeError(f"训记返回 HTTP {r.status_code}，请检查 Key 或权限")
        raw = r.content
        if raw[:2] == b"\x1f\x8b": raw = gzip.decompress(raw)
        try: result = json.loads(raw.decode("utf-8"))
        except Exception as e: raise RuntimeError("训记返回的不是有效 JSON") from e
        if isinstance(result, dict) and (result.get("error") or result.get("success") is False):
            raise RuntimeError("训记接口返回错误：" + str(result.get("error") or result.get("message") or result.get("msg") or "请求失败")[:160])
        cache[cache_id] = (time.monotonic()+cooldown, result)
        return result

@mcp.custom_route("/health", methods=["GET"])
async def health(_request: Request):
    return JSONResponse({"status":"ok","mode":"read-only","configured":{
        "training":bool(TRAIN_KEY),"food":bool(FOOD_KEY),"body":bool(BODY_KEY),"mcp_auth":bool(ACCESS)}})

@mcp.tool()
async def get_training_record(datestr: str, include_full_data: bool=False) -> dict:
    """读取指定日期的训练记录；完整模式包含 RPE、备注、未完成组、休息及心率摘要。只读。"""
    day(datestr)
    ttl = 30 if include_full_data else 15
    return await query(f"{TRAIN}/api_trains_for_llm_v2", TRAIN_KEY,
        {"schema_version":"train_open_api_v2","datestr":datestr,"include_full_data":include_full_data},
        f"train:{datestr}:{include_full_data}", f"train:{datestr}", ttl)

@mcp.tool()
async def get_body_records(start_date: str, end_date: str, types: list[str]|None=None,
                           include_latest: bool=True, include_records: bool=True,
                           limit: int=500, offset: int=0) -> dict:
    """读取体重、体脂率、腰围及其他围度。体重 kg、体脂率 %、围度 cm；腰围字段名为 weist。只读。"""
    date_range(start_date,end_date)
    allowed={"weight","bodyfat","neck","chest","weist","shoulder","bot","arm_left","arm_right",
             "forearm_left","forearm_right","leg_left","leg_right","cav_left","cav_right"}
    if types is not None and set(types)-allowed: raise ValueError("types 中包含未知身体数据类型")
    if not 1<=limit<=500 or offset<0: raise ValueError("limit 须为 1–500，offset 不可小于 0")
    p={"start_date":start_date,"end_date":end_date,"include_latest":include_latest,
       "include_records":include_records,"limit":limit,"offset":offset}
    if types is not None: p["types"]=types
    cid="body:"+json.dumps(p,sort_keys=True,ensure_ascii=False)
    return await query(f"{API}/open/body/query_gzip",BODY_KEY,p,cid,"body")

@mcp.tool()
async def get_food_records(start_date: str, end_date: str, include_detail: bool=True) -> dict:
    """读取饮食记录、餐次、食物与营养数据；范围限过去一年至未来三个月。只读。"""
    date_range(start_date,end_date,food=True)
    p={"start_date":start_date,"end_date":end_date,"include_detail":include_detail}
    cid="food:"+json.dumps(p,sort_keys=True,ensure_ascii=False)
    return await query(f"{FOOD}/open/food/query_gzip",FOOD_KEY,p,cid,"food")

@mcp.tool()
async def list_training_plans() -> dict:
    """列出训记当前正在使用的 PlatformPlan 和 UniversalPlan。只读。"""
    p={"schema_version":"plan_open_api_v1","action":"list"}
    return await query(f"{API}/open/plan/query_gzip",TRAIN_KEY,p,"plan:list","plan:list")

@mcp.tool()
async def get_training_plan(plan_ref: str, start_date: str|None=None, end_date: str|None=None,
                            include_movements: bool=True) -> dict:
    """读取指定训练计划的日历和动作。先调用 list_training_plans 取得 plan_ref；范围最多 92 天。只读。"""
    if not plan_ref or len(plan_ref)>160: raise ValueError("plan_ref 无效，请先列出计划")
    if (start_date is None)!=(end_date is None): raise ValueError("start_date 和 end_date 必须同时提供")
    p={"schema_version":"plan_open_api_v1","action":"get","plan_ref":plan_ref,"include_movements":include_movements}
    if start_date is not None:
        date_range(start_date,end_date,max_days=92)
        p.update(start_date=start_date,end_date=end_date)
    cid="plan:get:"+json.dumps(p,sort_keys=True,ensure_ascii=False)
    return await query(f"{API}/open/plan/query_gzip",TRAIN_KEY,p,cid,"plan:"+plan_ref)

class Auth:
    def __init__(self, app): self.app=app
    async def __call__(self, scope, receive, send):
        if scope["type"]!="http" or scope.get("path")=="/health":
            await self.app(scope,receive,send); return
        headers=dict(scope.get("headers",[]))
        auth=headers.get(b"authorization",b"").decode("latin-1")
        supplied=auth[7:].strip() if auth.startswith("Bearer ") else ""
        if not ACCESS or not hmac.compare_digest(supplied,ACCESS):
            body=b'{"error":"unauthorized"}'
            await send({"type":"http.response.start","status":401,"headers":[
                (b"content-type",b"application/json"),(b"content-length",str(len(body)).encode()),
                (b"www-authenticate",b"Bearer")]})
            await send({"type":"http.response.body","body":body}); return
        await self.app(scope,receive,send)

mcp_app=mcp.streamable_http_app(transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
app=Auth(mcp_app)
