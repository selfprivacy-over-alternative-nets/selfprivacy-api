# Upstream PR: Tor .onion Path-Based URL Routing

Branch ready at:
`https://github.com/selfprivacy-over-alternative-nets/selfprivacy-api/tree/upstream-pr/tor-onion-routing`

## To submit to git.selfprivacy.org

```bash
# 1. Create an account on git.selfprivacy.org and fork:
#    https://git.selfprivacy.org/SelfPrivacy/selfprivacy-rest-api

# 2. Add your fork as a remote (replace YOUR_USERNAME):
cd selfprivacy-api
git remote add gitea https://git.selfprivacy.org/YOUR_USERNAME/selfprivacy-rest-api.git

# 3. Push the PR branch to your fork:
git push gitea upstream-pr/tor-onion-routing

# 4. Open a PR at:
#    https://git.selfprivacy.org/YOUR_USERNAME/selfprivacy-rest-api/compare/master...upstream-pr/tor-onion-routing
```

## PR Title
```
feat: add Tor .onion path-based URL routing for hidden services
```

## PR Description
```markdown
When a SelfPrivacy server's domain ends in `.onion`, service URLs switch
from subdomain routing (`nextcloud.example.com`) to path-based routing
(`onion.example/nextcloud/`), so all services remain accessible through
a single Tor hidden service port.

### Changes

**New file: `selfprivacy_api/services/onion_routing.py`**
- `TOR_SERVICE_PATHS` dict mapping service IDs to nginx sub-paths:
  - nextcloud → `/nextcloud/`
  - gitea → `/git/`
  - matrix → `/_matrix/`
  - monitoring → `/prometheus/`
  - selfprivacy-api → `/api/`
  - jitsi-meet → `/jitsi/`

**Modified: `service.py`, `templated_service.py`, `services/__init__.py`, `prometheus/__init__.py`**
- `get_url()` checks `domain.endswith(".onion")` and returns path-based URL if true
- Non-.onion domains preserve existing subdomain routing unchanged

**New file: `tests/test_onion_routing.py`**
- 17 deterministic unit tests (T1.1–T1.17) covering all services and edge cases
- Mocked domain lookup — no system dependencies, runs in CI

### Backward compatibility

Non-.onion domains produce identical output to before this change.
All existing tests pass unchanged.

### Test results

```
tests/test_onion_routing.py::test_service_get_url_onion[nextcloud-/nextcloud/] PASSED
tests/test_onion_routing.py::test_service_get_url_onion[gitea-/git/] PASSED
tests/test_onion_routing.py::test_service_get_url_onion[matrix-/_matrix/] PASSED
tests/test_onion_routing.py::test_service_get_url_onion[monitoring-/prometheus/] PASSED
tests/test_onion_routing.py::test_service_get_url_onion[selfprivacy-api-/api/] PASSED
tests/test_onion_routing.py::test_service_get_url_onion[jitsi-meet-/jitsi/] PASSED
tests/test_onion_routing.py::test_service_get_url_normal_domain_nextcloud PASSED
tests/test_onion_routing.py::test_service_get_url_normal_domain_api PASSED
tests/test_onion_routing.py::test_service_get_url_unknown_id_no_crash PASSED
... (17 total, all PASSED)
```
```

## Files changed

```
selfprivacy_api/services/onion_routing.py       (new)  8 lines
selfprivacy_api/services/service.py              +6 lines
selfprivacy_api/services/templated_service.py    ±8 lines
selfprivacy_api/services/__init__.py             +2 lines
selfprivacy_api/services/prometheus/__init__.py  +4 lines
tests/test_onion_routing.py                     (new) 160 lines
```
