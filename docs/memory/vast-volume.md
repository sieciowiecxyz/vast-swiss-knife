# Vast volume copy

Purpose: Record verified behavior and current limits for standalone volume transfers.
Read when: Planning or verifying a Cloud Copy to a Vast volume.

- Situation: the API accepted a Google Drive to local volume copy and its result log contained only an object count and total bytes.
- Cause: the returned log exposed the source inventory, but did not show a completion marker or transferred byte count in the observed period.
- Verified: a separate small-file read from one volume only established its size. A later 120 GiB model copy was mounted on its GPU host: all 32 files passed `md5sum -c` against Google Drive, and Strata loaded the model and answered an API request.
- Use: treat an accepted request and an inventory-only log as an unknown outcome, not proof of completion. Verify files and checksums on the mounted volume before claiming success.
- Vast reported insufficient Google OAuth scope for the first connection. A second connection was able to enumerate the test file; keep access scope and host credential exposure in mind.
- Situation: some rentable GPU offers returned `no_such_ask` when rented, even though search listed them. Verify creation outcomes; a different GPU count on the same host was available in one observed case.
- Situation: creating a standalone volume returned an ID while its status was `initialized`; an immediate Cloud Copy was rejected with HTTP 400 `invalid_volume`.
- Verified: waiting for status `created` and then submitting the copy succeeded. Retry only this explicit rejection; a timeout or ambiguous error must remain an unknown outcome.
- Situation: search returned a rotating subset of offers for the same machine. A read-only recheck can miss a valid offer; up to three reads are safe, but never automatically retry a paid creation after an ambiguous result.
