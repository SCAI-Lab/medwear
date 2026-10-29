import asyncio
import logging
import os
import httpx

logger = logging.getLogger(__name__)


class GrafanaError(Exception):
    """Raised when a Grafana operation fails."""


GRAFANA_URL = os.getenv("GRAFANA_URL", "http://medwear-grafana:3000")
GRAFANA_USER = os.getenv("GF_SECURITY_ADMIN_USER", "")
GRAFANA_PASSWORD = os.getenv("GF_SECURITY_ADMIN_PASSWORD", "")

PG_HOST = os.getenv("POSTGRES_HOST", "medwear-postgres")
PG_PORT = int(os.getenv("POSTGRES_PORT", "5432"))
PG_USER = os.getenv("POSTGRES_USER", "medwear")
PG_PASS = os.getenv("POSTGRES_PASSWORD", "")

_service_token: str | None = None


async def _get_or_create_service_token() -> str:
    global _service_token
    if _service_token:
        return _service_token

    try:
        async with httpx.AsyncClient(base_url=GRAFANA_URL, auth=(GRAFANA_USER, GRAFANA_PASSWORD), timeout=10) as client:
            r = await client.get("/api/user")
            if r.status_code != 200:
                raise GrafanaError(f"Grafana authentication failed: {r.status_code} {r.text}")

            r2 = await client.get("/api/serviceaccounts/search?query=medwear-api-sync")
            accounts = r2.json().get("serviceAccounts", []) if r2.status_code == 200 else []
            sa_id = None
            for sa in accounts:
                if sa.get("name") == "medwear-api-sync":
                    sa_id = sa["id"]
                    break

            if not sa_id:
                r3 = await client.post("/api/serviceaccounts", json={"name": "medwear-api-sync", "role": "Admin"})
                if r3.status_code not in (200, 201):
                    raise GrafanaError(f"Failed to create service account: {r3.status_code} {r3.text}")
                sa_id = r3.json()["id"]

            r4 = await client.post(f"/api/serviceaccounts/{sa_id}/tokens", json={"name": "medwear-api-token"})
            if r4.status_code in (200, 201):
                _service_token = r4.json()["key"]
                logger.info("Grafana service account token created")
                return _service_token

            if r4.status_code in (400, 409):
                r5 = await client.get(f"/api/serviceaccounts/{sa_id}/tokens")
                tokens = r5.json() if r5.status_code == 200 else []
                for t in tokens:
                    await client.delete(f"/api/serviceaccounts/{sa_id}/tokens/{t['id']}")
                r6 = await client.post(f"/api/serviceaccounts/{sa_id}/tokens", json={"name": "medwear-api-token"})
                if r6.status_code in (200, 201):
                    _service_token = r6.json()["key"]
                    logger.info("Grafana service account token refreshed")
                    return _service_token

            raise GrafanaError(f"Failed to create service account token: {r4.status_code} {r4.text}")

    except GrafanaError:
        raise
    except Exception as e:
        raise GrafanaError(f"Unexpected error during Grafana token creation: {e}") from e


def _datasource_uid(project_name: str) -> str:
    return f"pg-{project_name}"


def _datasource_payload(project_name: str) -> dict:
    label = project_name.replace("medwear_", "").replace("_", " ").title()
    return {
        "name": f"PostgreSQL - {label}",
        "uid": _datasource_uid(project_name),
        "type": "grafana-postgresql-datasource",
        "access": "proxy",
        "url": f"{PG_HOST}:{PG_PORT}",
        "database": project_name,
        "user": PG_USER,
        "secureJsonData": {"password": PG_PASS},
        "jsonData": {"sslmode": "disable", "postgresVersion": 1500},
        "editable": True,
    }


async def _try_ensure(project_name: str) -> bool:
    global _service_token
    token = await _get_or_create_service_token()

    uid = _datasource_uid(project_name)
    payload = _datasource_payload(project_name)
    headers = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient(base_url=GRAFANA_URL, headers=headers, timeout=10) as client:
        r = await client.get(f"/api/datasources/uid/{uid}")

        if r.status_code == 401:
            _service_token = None
            return False

        if r.status_code == 200:
            r2 = await client.put(f"/api/datasources/uid/{uid}", json=payload)
            if r2.status_code in (200, 201):
                logger.info(f"Grafana datasource updated: {project_name}")
                return True
            logger.warning(f"Grafana datasource update failed for {project_name}: {r2.text}")
            return False

        if r.status_code == 404:
            r2 = await client.post("/api/datasources", json=payload)
            if r2.status_code in (200, 201):
                logger.info(f"Grafana datasource created: {project_name}")
                return True
            if r2.status_code == 409:
                r3 = await client.get(f"/api/datasources/name/{payload['name']}")
                if r3.status_code == 200:
                    r4 = await client.put(f"/api/datasources/uid/{uid}", json=payload)
                    if r4.status_code in (200, 201):
                        logger.info(f"Grafana datasource updated (conflict resolved): {project_name}")
                        return True
            logger.warning(f"Grafana datasource create failed for {project_name}: {r2.text}")
            return False

        logger.warning(f"Grafana datasource check failed for {project_name}: {r.text}")
        return False


async def ensure_datasource(project_name: str, retries: int = 3, delay: float = 5.0) -> bool:
    for attempt in range(1, retries + 1):
        try:
            result = await _try_ensure(project_name)
            if result:
                return True
        except Exception as e:
            logger.warning(f"Grafana ensure_datasource attempt {attempt}/{retries} failed for {project_name}: {e}")
        if attempt < retries:
            await asyncio.sleep(delay)
    logger.error(f"Grafana datasource could not be created for {project_name} after {retries} attempts")
    return False


async def delete_datasource(project_name: str) -> bool:
    token = await _get_or_create_service_token()
    uid = _datasource_uid(project_name)
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(base_url=GRAFANA_URL, headers=headers, timeout=10) as client:
        r = await client.delete(f"/api/datasources/uid/{uid}")
        if r.status_code in (200, 404):
            logger.info(f"Grafana datasource removed: {project_name}")
            return True
        logger.warning(f"Grafana datasource delete failed for {project_name}: {r.text}")
        return False


async def sync_all_datasources(projects: list) -> None:
    for project in projects:
        try:
            await ensure_datasource(project)
        except Exception as e:
            logger.warning(f"Grafana sync skipped for {project}: {e}")
