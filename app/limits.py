"""Hard limits shared by the test endpoint and the traffic generator.

The endpoint rejects requests above these limits, and the generator's config
validation refuses to plan requests the endpoint would reject.
"""

MAX_PAYLOAD_KB = 10 * 1024  # 10 MB, the upper end of the planned download profile
MAX_DELAY_MS = 30_000

# Response header that identifies the test endpoint. The generator refuses to
# run against a target that does not send it (see docs/DECISIONS.md, D-09).
TEST_ENDPOINT_HEADER = "X-QoE-Test-Endpoint"
TEST_ENDPOINT_VERSION = "poc-v1"
