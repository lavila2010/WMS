# WMS V2 Security Certification

Local adversarial suite: `tests/test_v2_phase09_security.py` plus IDOR tests in earlier phases.

| Check | Result |
|---|---|
| Anonymous routes | redirect login |
| Cross-client URL | 404 |
| Permission escalation POST | 403 |
| Password hashing | werkzeug hash |
| /health secrets | none |
| Secure cookies (production config) | HttpOnly, SameSite=Lax, Secure when env set |
| Concurrent allocation | one unit one winner |
| Concurrent lock | exact message |
| Duplicate scan | rejected |

Residual CSRF (no token library). Do not claim production PLATFORM_CERTIFIED until GATE 12–13 run on Render.
