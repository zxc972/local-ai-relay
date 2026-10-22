# Publishing an alpha

1. Run the local tests and fresh-wheel smoke tests. Build wheel and source distribution.
2. Inspect both archives. Exclude `.relay`, local MCP config, caches, logs, credentials and unrelated projects.
3. Create a dedicated GitHub repository from this project folder, not the parent workspace or mailbox history.
4. Run GitHub Actions on Windows, macOS and Linux. Claim platform verification only after actual passing results.
5. Verify host adapters separately. Queue acceptance is not Agent execution, and native consent is not tested by mocks.
6. Review the final source before tagging/releasing. Attach distributions and state limits: same-machine storage, explicit restart after reboot, host wake capability required.

The license is MIT. The repository is https://github.com/zxc972/local-ai-relay. Publish only verified CI and host-support claims. PyPI publication is separate; source and wheel installation already work.
