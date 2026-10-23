#!/usr/bin/bash
# Only the guarded, disposable integration agent can inject a failure.
exec /usr/bin/python3 /usr/libexec/mertensia-integration-agent health-fixture
