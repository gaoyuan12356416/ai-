# Owner-only Yue report

Serve `/reports/yuesiying/` using existing Feishu OAuth and existing API service.
Every HTML, script, stylesheet and JSON shard is gated by nginx auth_request.
Private report owner configuration is local `config/private_report_owners.json`,
with `yuesiying: {user_id, tenant_key}`. Missing config/auth fails closed.
API tokens and administrator roles do not bypass the owner allowlist.
Do not publish refreshed snapshots as public COS artifacts. Store them privately
and preserve the authenticated serving path. Never expose the configured alias
directory through another static location. Older public COS copies must have
their object ACL changed to private and anonymous GET verified.

Deployment: insert the following unique branch in `DramaMaterialHandler.do_GET`
after parsing the path, before the existing tt-minis report auth branch:

```python
        if parsed.path == "/api/report-auth/yuesiying":
            from features.private_reports import authorize
            authorize(self, globals())
            return
```

Preserve all other live app.py changes; deploy only this branch, the feature
module and this nginx include. Back up live app.py and configuration first.
Validate Python compilation, unit tests, nginx -t, unauthenticated requests,
owner/foreign-user authorization and direct asset gating before disabling old
URLs. Rollback must keep report access closed; never restore public COS ACLs
merely to restore service availability.
