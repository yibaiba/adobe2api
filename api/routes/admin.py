import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, List

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from starlette.responses import RedirectResponse

from api.schemas import (
    AdminLoginRequest,
    ConfigUpdateRequest,
    ExportSelectionRequest,
    RefreshCookieBatchImportRequest,
    RefreshCookieImportRequest,
    RefreshProfileBatchImportRequest,
    RefreshProfileEnabledRequest,
    RefreshProfileImportRequest,
    TokenAddRequest,
    TokenBatchAddRequest,
    TokenCreditsBatchRefreshRequest,
)


def build_admin_router(
    *,
    static_dir: Path,
    token_manager,
    config_manager,
    refresh_manager,
    log_store,
    live_log_store,
    require_admin_auth: Callable[[Request], None],
    is_admin_authenticated: Callable[[Request], bool],
    apply_client_config: Callable[[], None],
) -> APIRouter:
    router = APIRouter()

    @router.get("/api/v1/health")
    def health():
        return {"status": "ok", "pool_size": len(token_manager.list_all())}

    @router.get("/login", include_in_schema=False)
    def page_login(request: Request):
        if is_admin_authenticated(request):
            return RedirectResponse(url="/")
        return FileResponse(static_dir / "login.html")

    @router.post("/api/v1/auth/login")
    def admin_login(req: AdminLoginRequest, request: Request):
        username = str(req.username or "").strip()
        password = str(req.password or "")
        expected_username = str(
            config_manager.get("admin_username", "admin") or "admin"
        ).strip()
        expected_password = str(
            config_manager.get("admin_password", "admin") or "admin"
        )

        if username != expected_username or password != expected_password:
            raise HTTPException(status_code=401, detail="Invalid username or password")

        request.session.clear()
        request.session["admin_auth"] = True
        request.session["username"] = username
        request.session["login_at"] = int(time.time())
        return {"status": "ok", "username": username}

    @router.get("/api/v1/auth/me")
    def admin_me(request: Request):
        if not is_admin_authenticated(request):
            raise HTTPException(status_code=401, detail="Unauthorized")
        return {
            "authenticated": True,
            "username": str((request.session or {}).get("username") or ""),
        }

    @router.post("/api/v1/auth/logout")
    def admin_logout(request: Request):
        request.session.clear()
        return {"status": "ok"}

    @router.get("/", include_in_schema=False)
    def page_root(request: Request):
        if not is_admin_authenticated(request):
            return RedirectResponse(url="/login")
        return FileResponse(static_dir / "admin.html")

    @router.get("/api/v1/logs")
    def list_logs(request: Request, limit: int = 20, page: int = 1):
        require_admin_auth(request)
        logs, total = log_store.list(limit=limit, page=page)
        safe_limit = min(max(int(limit or 20), 1), 100)
        safe_page = max(int(page or 1), 1)
        total_pages = (total + safe_limit - 1) // safe_limit if total > 0 else 1
        if safe_page > total_pages:
            safe_page = total_pages
        return {
            "logs": logs,
            "page": safe_page,
            "limit": safe_limit,
            "total": total,
            "total_pages": total_pages,
        }

    @router.get("/api/v1/logs/running")
    def list_running_logs(request: Request, limit: int = 200):
        require_admin_auth(request)
        rows = live_log_store.list(limit=limit)
        items = []
        for item in rows:
            if not isinstance(item, dict):
                continue
            status = str(item.get("task_status") or "").upper()
            if status != "IN_PROGRESS":
                continue
            items.append(item)
        return {"items": items, "total": len(items)}

    def _resolve_logs_stats_range(range_key: str) -> tuple[str, float, float]:
        now_dt = datetime.now()
        now_ts = time.time()
        key = str(range_key or "today").strip().lower()
        if key == "today":
            start_dt = datetime(now_dt.year, now_dt.month, now_dt.day)
        elif key == "7d":
            start_dt = now_dt - timedelta(days=7)
        elif key == "30d":
            start_dt = now_dt - timedelta(days=30)
        else:
            raise HTTPException(
                status_code=400, detail="range must be one of: today, 7d, 30d"
            )
        return key, start_dt.timestamp(), now_ts

    @router.get("/api/v1/logs/stats")
    def logs_stats(request: Request, range: str = "today"):
        require_admin_auth(request)
        range_key, start_ts, end_ts = _resolve_logs_stats_range(range)
        payload = log_store.stats(start_ts=start_ts, end_ts=end_ts)
        payload["in_progress_requests"] = live_log_store.count_in_progress()
        payload.update({"range": range_key, "start_ts": start_ts, "end_ts": end_ts})
        return payload

    @router.delete("/api/v1/logs")
    def clear_logs(request: Request):
        require_admin_auth(request)
        log_store.clear()
        return {"status": "ok"}

    @router.get("/api/v1/tokens")
    def list_tokens(request: Request):
        require_admin_auth(request)
        tokens = token_manager.list_all()
        for item in tokens:
            if not bool(item.get("auto_refresh")):
                item["auto_refresh_enabled"] = None
                continue
            pid = str(item.get("refresh_profile_id") or "").strip()
            item["auto_refresh_enabled"] = refresh_manager.is_profile_enabled(pid)
        total_count = len(tokens)
        active_count = 0
        for item in tokens:
            if str(item.get("status") or "").strip().lower() == "active":
                active_count += 1
        return {
            "tokens": tokens,
            "summary": {
                "total": total_count,
                "active": active_count,
            },
        }

    @router.post("/api/v1/tokens")
    def add_token(req: TokenAddRequest, request: Request):
        require_admin_auth(request)
        if not req.token.strip():
            raise HTTPException(status_code=400, detail="Empty token")
        token_manager.add(req.token)
        return {"status": "ok"}

    @router.post("/api/v1/tokens/batch")
    def add_tokens_batch(req: TokenBatchAddRequest, request: Request):
        require_admin_auth(request)
        if not req.tokens:
            raise HTTPException(status_code=400, detail="tokens is required")

        added_count = 0
        for raw in req.tokens:
            token = str(raw or "").strip()
            if not token:
                continue
            token_manager.add(token)
            added_count += 1

        if added_count == 0:
            raise HTTPException(status_code=400, detail="no valid token provided")

        return {"status": "ok", "added_count": added_count}

    @router.post("/api/v1/tokens/export")
    def export_tokens(req: ExportSelectionRequest, request: Request):
        require_admin_auth(request)
        token_ids = req.ids if isinstance(req.ids, list) else None
        exported = token_manager.export_tokens(token_ids)
        return {
            "status": "ok",
            "total": len(exported),
            "selected": bool(token_ids),
            "tokens": exported,
        }

    @router.delete("/api/v1/tokens/{tid}")
    def delete_token(tid: str, request: Request):
        require_admin_auth(request)
        token_manager.remove(tid)
        return {"status": "ok"}

    @router.put("/api/v1/tokens/{tid}/status")
    def set_token_status(tid: str, status: str, request: Request):
        require_admin_auth(request)
        if status not in ("active", "disabled"):
            raise HTTPException(status_code=400, detail="Invalid status")
        token_info = token_manager.get_by_id(tid)
        if not token_info:
            raise HTTPException(status_code=404, detail="token not found")
        if status == "active" and token_info.get("status") in {"exhausted", "invalid"}:
            raise HTTPException(
                status_code=400,
                detail="exhausted/invalid token cannot be reactivated; replace with a fresh token",
            )
        token_manager.set_status(tid, status)
        return {"status": "ok"}

    @router.post("/api/v1/tokens/{tid}/refresh")
    def refresh_token_now(tid: str, request: Request):
        require_admin_auth(request)
        token_info = token_manager.get_by_id(tid)
        if not token_info:
            raise HTTPException(status_code=404, detail="token not found")

        profile_id = str(token_info.get("refresh_profile_id") or "").strip()
        if not profile_id:
            raise HTTPException(
                status_code=400,
                detail="this token is not bound to an auto refresh profile",
            )

        try:
            result = refresh_manager.refresh_once(profile_id)
            return {"status": "ok", "result": result}
        except KeyError:
            raise HTTPException(status_code=404, detail="refresh profile not found")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    @router.put("/api/v1/tokens/{tid}/auto-refresh")
    def set_token_auto_refresh_enabled(tid: str, enabled: bool, request: Request):
        require_admin_auth(request)
        token_info = token_manager.get_by_id(tid)
        if not token_info:
            raise HTTPException(status_code=404, detail="token not found")

        profile_id = str(token_info.get("refresh_profile_id") or "").strip()
        if not profile_id:
            raise HTTPException(
                status_code=400,
                detail="this token is not bound to an auto refresh profile",
            )
        try:
            profile = refresh_manager.set_enabled(profile_id, bool(enabled))
            return {"status": "ok", "profile": profile}
        except KeyError:
            raise HTTPException(status_code=404, detail="refresh profile not found")

    @router.post("/api/v1/tokens/{tid}/credits/refresh")
    def refresh_token_credits(tid: str, request: Request):
        require_admin_auth(request)
        token_info = token_manager.get_by_id(tid)
        if not token_info:
            raise HTTPException(status_code=404, detail="token not found")
        try:
            result = refresh_manager.refresh_credits_for_token_id(tid)
            return {"status": "ok", **result}
        except KeyError:
            raise HTTPException(status_code=404, detail="token not found")
        except Exception as exc:
            token_manager.set_credits_error(tid, str(exc))
            raise HTTPException(status_code=500, detail=str(exc))

    @router.post("/api/v1/tokens/credits/refresh-batch")
    def refresh_tokens_credits_batch(
        req: TokenCreditsBatchRefreshRequest, request: Request
    ):
        require_admin_auth(request)
        ids = req.ids if isinstance(req.ids, list) else None
        token_ids: List[str] = []
        if ids:
            token_ids = [str(x or "").strip() for x in ids if str(x or "").strip()]
        else:
            token_ids = token_manager.list_active_ids()

        if not token_ids:
            raise HTTPException(status_code=400, detail="no token to refresh")

        refreshed = []
        failed = []
        for tid in token_ids:
            try:
                refreshed.append(refresh_manager.refresh_credits_for_token_id(tid))
            except Exception as exc:
                token_manager.set_credits_error(tid, str(exc))
                failed.append({"token_id": tid, "detail": str(exc)})

        return {
            "status": "ok" if not failed else "partial",
            "total": len(token_ids),
            "refreshed_count": len(refreshed),
            "failed_count": len(failed),
            "refreshed": refreshed,
            "failed": failed,
        }

    @router.get("/api/v1/config")
    def get_config(request: Request):
        require_admin_auth(request)
        cfg = config_manager.get_all()
        cfg.pop("admin_session_secret", None)
        return cfg

    @router.put("/api/v1/config")
    def update_config(req: ConfigUpdateRequest, request: Request):
        require_admin_auth(request)
        incoming = req.model_dump(exclude_unset=True)
        if not incoming:
            return config_manager.get_all()

        update_data = {}
        if "api_key" in incoming:
            update_data["api_key"] = str(incoming["api_key"] or "").strip()
        if "admin_username" in incoming:
            admin_username = str(incoming["admin_username"] or "").strip()
            if not admin_username:
                raise HTTPException(
                    status_code=400, detail="admin_username cannot be empty"
                )
            update_data["admin_username"] = admin_username
        if "admin_password" in incoming:
            admin_password = str(incoming["admin_password"] or "")
            if not admin_password:
                raise HTTPException(
                    status_code=400, detail="admin_password cannot be empty"
                )
            update_data["admin_password"] = admin_password
        if "public_base_url" in incoming:
            update_data["public_base_url"] = str(
                incoming["public_base_url"] or ""
            ).strip()
        if "proxy" in incoming:
            update_data["proxy"] = str(incoming["proxy"] or "").strip()
        if "use_proxy" in incoming:
            update_data["use_proxy"] = bool(incoming["use_proxy"])
        if "generate_timeout" in incoming:
            try:
                timeout_val = int(incoming["generate_timeout"])
            except Exception:
                timeout_val = 300
            update_data["generate_timeout"] = timeout_val if timeout_val > 0 else 300
        if "refresh_interval_hours" in incoming:
            try:
                interval_hours = int(incoming["refresh_interval_hours"])
            except Exception:
                raise HTTPException(
                    status_code=400,
                    detail="refresh_interval_hours must be an integer between 1 and 24",
                )
            if interval_hours < 1 or interval_hours > 24:
                raise HTTPException(
                    status_code=400,
                    detail="refresh_interval_hours must be between 1 and 24",
                )
            update_data["refresh_interval_hours"] = interval_hours
        if "retry_enabled" in incoming:
            update_data["retry_enabled"] = bool(incoming["retry_enabled"])
        if "retry_max_attempts" in incoming:
            try:
                retry_max_attempts = int(incoming["retry_max_attempts"])
            except Exception:
                raise HTTPException(
                    status_code=400, detail="retry_max_attempts must be an integer"
                )
            if retry_max_attempts < 1 or retry_max_attempts > 10:
                raise HTTPException(
                    status_code=400,
                    detail="retry_max_attempts must be between 1 and 10",
                )
            update_data["retry_max_attempts"] = retry_max_attempts
        if "retry_backoff_seconds" in incoming:
            try:
                retry_backoff_seconds = float(incoming["retry_backoff_seconds"])
            except Exception:
                raise HTTPException(
                    status_code=400,
                    detail="retry_backoff_seconds must be a number",
                )
            if retry_backoff_seconds < 0 or retry_backoff_seconds > 30:
                raise HTTPException(
                    status_code=400,
                    detail="retry_backoff_seconds must be between 0 and 30",
                )
            update_data["retry_backoff_seconds"] = retry_backoff_seconds
        if "retry_on_status_codes" in incoming:
            raw_codes = incoming["retry_on_status_codes"] or []
            if not isinstance(raw_codes, list):
                raise HTTPException(
                    status_code=400, detail="retry_on_status_codes must be a list"
                )
            status_codes: list[int] = []
            for item in raw_codes:
                try:
                    code = int(item)
                except Exception:
                    raise HTTPException(
                        status_code=400,
                        detail="retry_on_status_codes contains invalid value",
                    )
                if code < 100 or code > 599:
                    raise HTTPException(
                        status_code=400,
                        detail="retry_on_status_codes must be HTTP status codes",
                    )
                status_codes.append(code)
            update_data["retry_on_status_codes"] = sorted(set(status_codes))
        if "retry_on_error_types" in incoming:
            raw_types = incoming["retry_on_error_types"] or []
            if not isinstance(raw_types, list):
                raise HTTPException(
                    status_code=400, detail="retry_on_error_types must be a list"
                )
            error_types: list[str] = []
            for item in raw_types:
                txt = str(item or "").strip().lower()
                if txt:
                    error_types.append(txt)
            update_data["retry_on_error_types"] = sorted(set(error_types))
        if "token_rotation_strategy" in incoming:
            strategy = str(incoming["token_rotation_strategy"] or "").strip().lower()
            if strategy not in {"round_robin", "random"}:
                raise HTTPException(
                    status_code=400,
                    detail="token_rotation_strategy must be one of: round_robin, random",
                )
            update_data["token_rotation_strategy"] = strategy
        config_manager.update_all(update_data)
        apply_client_config()
        return config_manager.get_all()

    @router.get("/api/v1/refresh-profiles")
    def refresh_profiles_list(request: Request):
        require_admin_auth(request)
        return {"profiles": refresh_manager.list_profiles()}

    @router.post("/api/v1/refresh-profiles/export")
    def refresh_profiles_export(req: ExportSelectionRequest, request: Request):
        require_admin_auth(request)
        profile_ids = req.ids if isinstance(req.ids, list) else None
        exported = refresh_manager.export_bundles(profile_ids)
        return {
            "status": "ok",
            "total": len(exported),
            "selected": bool(profile_ids),
            "items": exported,
        }

    @router.post("/api/v1/refresh-profiles/export-cookies")
    def refresh_profiles_export_cookies(req: ExportSelectionRequest, request: Request):
        require_admin_auth(request)
        profile_ids = req.ids if isinstance(req.ids, list) else None
        exported = refresh_manager.export_cookies(profile_ids)
        return {
            "status": "ok",
            "total": len(exported),
            "selected": bool(profile_ids),
            "items": exported,
        }

    @router.post("/api/v1/refresh-profiles/import")
    def refresh_profiles_import(req: RefreshProfileImportRequest, request: Request):
        require_admin_auth(request)
        try:
            profile = refresh_manager.import_bundle(req.bundle, name=req.name)
            refresh_result = None
            refresh_error = ""
            try:
                refresh_result = refresh_manager.refresh_once(
                    str(profile.get("id") or "")
                )
            except Exception as exc:
                refresh_error = str(exc)
            return {
                "status": "ok" if not refresh_error else "partial",
                "profile": profile,
                "refresh_result": refresh_result,
                "refresh_error": refresh_error,
            }
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @router.post("/api/v1/refresh-profiles/import-cookie")
    def refresh_profiles_import_cookie(
        req: RefreshCookieImportRequest, request: Request
    ):
        require_admin_auth(request)
        try:
            profile = refresh_manager.import_cookie(req.cookie, name=req.name)
            refresh_result = None
            refresh_error = ""
            try:
                refresh_result = refresh_manager.refresh_once(
                    str(profile.get("id") or "")
                )
            except Exception as exc:
                refresh_error = str(exc)
            return {
                "status": "ok" if not refresh_error else "partial",
                "profile": profile,
                "refresh_result": refresh_result,
                "refresh_error": refresh_error,
            }
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @router.post("/api/v1/refresh-profiles/import-batch")
    def refresh_profiles_import_batch(
        req: RefreshProfileBatchImportRequest, request: Request
    ):
        require_admin_auth(request)
        if not req.items:
            raise HTTPException(status_code=400, detail="items is required")

        imported = []
        failed = []
        refreshed = []
        refresh_failed = []
        for idx, item in enumerate(req.items):
            try:
                profile = refresh_manager.import_bundle(item.bundle, name=item.name)
                imported.append(profile)
                try:
                    refresh_result = refresh_manager.refresh_once(
                        str(profile.get("id") or "")
                    )
                    refreshed.append(
                        {
                            "index": idx,
                            "profile_id": profile.get("id"),
                            "profile_name": profile.get("name"),
                            "result": refresh_result,
                        }
                    )
                except Exception as exc:
                    refresh_failed.append(
                        {
                            "index": idx,
                            "profile_id": profile.get("id"),
                            "profile_name": profile.get("name"),
                            "detail": str(exc),
                        }
                    )
            except ValueError as exc:
                failed.append(
                    {
                        "index": idx,
                        "name": item.name,
                        "detail": str(exc),
                    }
                )

        result = {
            "status": (
                "ok"
                if (not failed and not refresh_failed)
                else ("partial" if imported else "failed")
            ),
            "total": len(req.items),
            "imported_count": len(imported),
            "failed_count": len(failed),
            "refreshed_count": len(refreshed),
            "refresh_failed_count": len(refresh_failed),
            "profiles": imported,
            "failed": failed,
            "refreshed": refreshed,
            "refresh_failed": refresh_failed,
        }
        if not imported:
            raise HTTPException(status_code=400, detail=result)
        return result

    @router.post("/api/v1/refresh-profiles/import-cookie-batch")
    def refresh_profiles_import_cookie_batch(
        req: RefreshCookieBatchImportRequest, request: Request
    ):
        require_admin_auth(request)
        if not req.items:
            raise HTTPException(status_code=400, detail="items is required")

        imported = []
        failed = []
        refreshed = []
        refresh_failed = []
        for idx, item in enumerate(req.items):
            try:
                profile = refresh_manager.import_cookie(item.cookie, name=item.name)
                imported.append(profile)
                try:
                    refresh_result = refresh_manager.refresh_once(
                        str(profile.get("id") or "")
                    )
                    refreshed.append(
                        {
                            "index": idx,
                            "profile_id": profile.get("id"),
                            "profile_name": profile.get("name"),
                            "result": refresh_result,
                        }
                    )
                except Exception as exc:
                    refresh_failed.append(
                        {
                            "index": idx,
                            "profile_id": profile.get("id"),
                            "profile_name": profile.get("name"),
                            "detail": str(exc),
                        }
                    )
            except ValueError as exc:
                failed.append(
                    {
                        "index": idx,
                        "name": item.name,
                        "detail": str(exc),
                    }
                )

        result = {
            "status": (
                "ok"
                if (not failed and not refresh_failed)
                else ("partial" if imported else "failed")
            ),
            "total": len(req.items),
            "imported_count": len(imported),
            "failed_count": len(failed),
            "refreshed_count": len(refreshed),
            "refresh_failed_count": len(refresh_failed),
            "profiles": imported,
            "failed": failed,
            "refreshed": refreshed,
            "refresh_failed": refresh_failed,
        }
        if not imported:
            raise HTTPException(status_code=400, detail=result)
        return result

    @router.post("/api/v1/refresh-profiles/{profile_id}/refresh-now")
    def refresh_profiles_refresh_now(profile_id: str, request: Request):
        require_admin_auth(request)
        try:
            return refresh_manager.refresh_once(profile_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="profile not found")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    @router.put("/api/v1/refresh-profiles/{profile_id}/enabled")
    def refresh_profiles_set_enabled(
        profile_id: str, req: RefreshProfileEnabledRequest, request: Request
    ):
        require_admin_auth(request)
        try:
            profile = refresh_manager.set_enabled(profile_id, req.enabled)
            return {"status": "ok", "profile": profile}
        except KeyError:
            raise HTTPException(status_code=404, detail="profile not found")

    @router.delete("/api/v1/refresh-profiles/{profile_id}")
    def refresh_profiles_delete(profile_id: str, request: Request):
        require_admin_auth(request)
        try:
            refresh_manager.remove_profile(profile_id)
            return {"status": "ok"}
        except KeyError:
            raise HTTPException(status_code=404, detail="profile not found")

    return router
