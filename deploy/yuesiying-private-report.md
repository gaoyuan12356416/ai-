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

COS defense in depth: retain the bucket's existing statements and add only a
scoped `Deny anyone` GetObject/HeadObject statement for verified report version
prefixes. Read back and compare resource sets (COS reorders resources).
This deployment also protects future `yuesiying-drama-*` and
`yuesiying-language-select-all-*` HTML prefixes. Use the live policy's verified
resource namespace (`uid/1306474899`), not an assumed `appid/` template.
Policy propagation is not immediate: confirm anonymous GET returns 403 for
every historical HTML and sample data shards, including cache-busting URLs.
Signed administrative storage access remains available; end users access
the report through the Feishu-gated domain route, never a presigned COS URL.

Owner identity was verified against the exact requesting Feishu message and
the current backend user record; the tenant-scoped user_id is allowlisted.
Supported existing login session sources are `feishu_web` and
`feishu_internal`. No admin-role or bearer-token bypass is permitted.
