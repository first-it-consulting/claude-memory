# Changelog

## [0.1.2](https://github.com/first-it-consulting/claude-memory/compare/v0.1.1...v0.1.2) (2026-11-21)


### Features

* exact-identifier keyword path with hybrid RRF ranking ([388eb2c](https://github.com/first-it-consulting/claude-memory/commit/388eb2c5ba96b54c44dddb071cd0c97608715e1d))
* **hooks:** token-budgeted compact-index SessionStart briefing ([cd55039](https://github.com/first-it-consulting/claude-memory/commit/cd5503934f5bf43b7fe9449a28c4d585705d62f8))
* OpenAI-compatible embedder/LLM providers with bearer auth ([cd326b7](https://github.com/first-it-consulting/claude-memory/commit/cd326b762a47ba8a10c9eda62ca5fe8e0de63a42))
* setup and doctor script for harness configs ([a24725a](https://github.com/first-it-consulting/claude-memory/commit/a24725abcdcc47d4a8d5591cf576c62bb5950a24))
* token-efficient search - index mode, fetch-by-ids, timeline ([34f7287](https://github.com/first-it-consulting/claude-memory/commit/34f7287f917ad16af58c5ce11b2e5e528691a407))


### Bug Fixes

* **ci:** pass -R to gh workflow run in publish trigger ([0966d63](https://github.com/first-it-consulting/claude-memory/commit/0966d63b3a2e0d5f38886abd8c2931116d0b929a))
* first-write pgvector catalog race drops queue jobs ([e018baf](https://github.com/first-it-consulting/claude-memory/commit/e018baffd436a887eeaf61e71d770daa4ddd5b29))
* **stats:** report distinct tagged memories in /api/stats ([a3227a0](https://github.com/first-it-consulting/claude-memory/commit/a3227a013966b089535c8e61df9ea944db325923))

## [0.1.1](https://github.com/first-it-consulting/claude-memory/compare/v0.1.0...v0.1.1) (2026-11-21)


### Bug Fixes

* **ci:** trigger publish for release-please-created tags ([#5](https://github.com/first-it-consulting/claude-memory/issues/5)) ([64773a5](https://github.com/first-it-consulting/claude-memory/commit/64773a5c90c06da64512939badf1f92e3a0064ed))
* **renovate:** disable all python/node runtime PRs (manual only) ([#13](https://github.com/first-it-consulting/claude-memory/issues/13)) ([7a5e17f](https://github.com/first-it-consulting/claude-memory/commit/7a5e17fd14f4807e806add3a49535e72bf157765))

## 0.1.0 (2026-11-21)


### Features

* **api:** ?tag= filter on GET /api/memories (e.g. tag=auto-promoted) ([60b6885](https://github.com/first-it-consulting/claude-memory/commit/60b68853ff80d0a4454bc63a9f07ff27316fa533))
* **api:** multi-tenant workspaces with per-user auth and shared space ([b3c53ca](https://github.com/first-it-consulting/claude-memory/commit/b3c53ca8c9490f07ffc3fe656d85109553ba86e5))
* DELETE /api/sessions/{id} and harness filter for session lists ([ab24c98](https://github.com/first-it-consulting/claude-memory/commit/ab24c98ab2fb779ca7e93505faddc85d360fae66))
* DELETE /api/sessions/{id} and harness filter for session lists ([f6d3d27](https://github.com/first-it-consulting/claude-memory/commit/f6d3d27ad338f509b3737ab250bfa31aa369ad3c))
* expose version and commit via /api/version ([#8](https://github.com/first-it-consulting/claude-memory/issues/8)) ([333d1c0](https://github.com/first-it-consulting/claude-memory/commit/333d1c0e5bb3093ba56e780628eec47fb18fbf1c))
* **mcp:** optional harness argument on the checkpoint tool ([abe3a9b](https://github.com/first-it-consulting/claude-memory/commit/abe3a9b38f786337195fc2baa72e638b80aba7aa))
* **mcp:** optional harness argument on the checkpoint tool ([4736830](https://github.com/first-it-consulting/claude-memory/commit/4736830031b22f82d5911f52f9cf7aaa1dcea4a9))
* multi-tenant workspaces with per-user auth and shared space ([fa91944](https://github.com/first-it-consulting/claude-memory/commit/fa91944de0389ad6263331d07281695d2883557d))
* **ops:** re-tenant tooling for legacy default data (decision needed) ([21a85bf](https://github.com/first-it-consulting/claude-memory/commit/21a85bf9a9090eed1eaeda4498c56a52f3c9173b))
* **ops:** re-tenant tooling for the legacy default workspace (dry-run only so far) ([e3b8e8f](https://github.com/first-it-consulting/claude-memory/commit/e3b8e8f70f0acc2bb91d548e88d958d94f939711))
* promote checkpoint decisions and dead ends into long-term memory ([48bdd8c](https://github.com/first-it-consulting/claude-memory/commit/48bdd8c9fd4c0f47598988e11b06438d2019db7c))
* promote checkpoint decisions and dead ends into long-term memory ([9ffe23b](https://github.com/first-it-consulting/claude-memory/commit/9ffe23be3f655468546a74a2d66951fc41aa5d91))
* release automation with release-please ([#53](https://github.com/first-it-consulting/claude-memory/issues/53)) ([751d17a](https://github.com/first-it-consulting/claude-memory/commit/751d17ae27d7d99e735f2d3a3955db0d8c4bfcf3))
* session working memory + harness hook adapters ([2477fee](https://github.com/first-it-consulting/claude-memory/commit/2477fee368e2ddcad35673e4d8da51af77012ab7))
* session working memory + harness hook adapters (+ tests, CI) ([47190b6](https://github.com/first-it-consulting/claude-memory/commit/47190b692daafaa95aad63377dc21c186538937e))
* **tokens:** per-user API tokens (api_tokens) with self-service and admin views ([0c79007](https://github.com/first-it-consulting/claude-memory/commit/0c79007c6294233a474de64e5c7e90770e9765ce))
* **tokens:** per-user API tokens with self-service and admin views (P1) ([b0e855a](https://github.com/first-it-consulting/claude-memory/commit/b0e855a02709221a0b0ec2795f2863e23b813909))
* **tokens:** register shared bearer as legacy-hooks with real revocation ([6163528](https://github.com/first-it-consulting/claude-memory/commit/616352875d028044bc5381df0b8f09dcea3338a4))
* **tokens:** shared bearer as legacy-hooks with real revocation (P3) ([5e33cd4](https://github.com/first-it-consulting/claude-memory/commit/5e33cd47e37646b6e35dd08086e9fbe1ca2b6785))


### Bug Fixes

* add_memory never drops a note silently ([6eed7d8](https://github.com/first-it-consulting/claude-memory/commit/6eed7d81cc731b08e1db6e8296ab00e9c11d24ab))
* add_memory never loses a note (store as written, fallback, logging) ([81b0976](https://github.com/first-it-consulting/claude-memory/commit/81b097676f42117a4315bbde27af51705c2154e2))
* add_memory stores agent notes as written by default ([3f148d8](https://github.com/first-it-consulting/claude-memory/commit/3f148d82b56eb60ec7b74102bf32f8ba8587a600))
* BEARER_TOKEN code default is empty, disabling legacy auth ([#87](https://github.com/first-it-consulting/claude-memory/issues/87)) ([55c3168](https://github.com/first-it-consulting/claude-memory/commit/55c3168b3b4cf869af1e056e47179a3158fb6a77))
* bound Ollama request time (OLLAMA_TIMEOUT, default 300s) ([eed10b1](https://github.com/first-it-consulting/claude-memory/commit/eed10b1693c3764f8d42e36342ba9672d56143f9))
* clean pre-compact capture, shorter recall, sessions keep their project ([a868c24](https://github.com/first-it-consulting/claude-memory/commit/a868c2429c792bd3f2969c1daa15707196dfee70))
* clean pre-compact capture, shorter recall, sessions keep their project ([1f1c00c](https://github.com/first-it-consulting/claude-memory/commit/1f1c00cd696e78b891e3ddd0c9bb6fc49f85d9d0))
* correct codeql-action pin to the v3.39.4 commit SHA ([#86](https://github.com/first-it-consulting/claude-memory/issues/86)) ([6c17161](https://github.com/first-it-consulting/claude-memory/commit/6c17161c2b5a546f20fd87462ef51a2d43997dc1))
* **dedup:** cast row ids to uuid; roll back before recording a failure ([a615da0](https://github.com/first-it-consulting/claude-memory/commit/a615da006fe2f1324fe76a2020ccfae5fb30011d))
* **dedup:** cast row ids to uuid; roll back before recording a failure ([e3b3e09](https://github.com/first-it-consulting/claude-memory/commit/e3b3e0980dc2939385f55089971506b19bfa4d0f))
* **dedup:** keep the model's own remarks out of merged memories ([9a6526c](https://github.com/first-it-consulting/claude-memory/commit/9a6526cad59ed31fd8d854edceb6736666516400))
* **dedup:** keep the model's own remarks out of merged memories ([ad8eafe](https://github.com/first-it-consulting/claude-memory/commit/ad8eafeef961015c51c0c8a6796cf420290a8c2d))
* **dedup:** semantic phase never merged; never delete without a stored replacement ([1ee99cc](https://github.com/first-it-consulting/claude-memory/commit/1ee99cc242bbf46f78e18b4075764e628341443d))
* **dedup:** semantic phase never merged; never delete without a stored replacement ([f2475bf](https://github.com/first-it-consulting/claude-memory/commit/f2475bf14eb327f5a5385c3f421e8702362feca6))
* don't log memory content for failed queue jobs ([ee8f7f6](https://github.com/first-it-consulting/claude-memory/commit/ee8f7f676f761599d08ba6fbcb9de040e4a98580))
* gitleaks config extends built-in ruleset ([extend] useDefault) ([#92](https://github.com/first-it-consulting/claude-memory/issues/92)) ([c513ec4](https://github.com/first-it-consulting/claude-memory/commit/c513ec4c829343606e94bad93907c2882c1663e8))
* gitleaks custom rule for real token shape, narrow allowlist ([#88](https://github.com/first-it-consulting/claude-memory/issues/88)) ([4bcf362](https://github.com/first-it-consulting/claude-memory/commit/4bcf3621a812f9bc4a679a38929969ad311be8d3))
* **image:** copy all src/scripts modules; test that the image has every module ([b1349bd](https://github.com/first-it-consulting/claude-memory/commit/b1349bd16aa36fa60bc1cc68ebcec917649ce7f5))
* **image:** copy all src/scripts modules; test that the image has every module ([2d8e1ad](https://github.com/first-it-consulting/claude-memory/commit/2d8e1ad7af990a2c2810940233a0054d40f3921d))
* keep event loop free of blocking mem0/Ollama calls ([757acb3](https://github.com/first-it-consulting/claude-memory/commit/757acb37a6fe9994f08781b37155f8af44648862))
* publish quickstart port on 127.0.0.1 by default (APP_BIND override) ([#89](https://github.com/first-it-consulting/claude-memory/issues/89)) ([2b2721f](https://github.com/first-it-consulting/claude-memory/commit/2b2721f141c82292c34a74567292b4c75c4f045e))
* **tokens:** admin HTTP revoke test, shared-token last_used, startup init ([96dafe3](https://github.com/first-it-consulting/claude-memory/commit/96dafe37ae2fffad80971edc2bb26d14c89e20b3))
* **tokens:** include token_hash in resolve SELECT; robust shape assertion ([34a5ca1](https://github.com/first-it-consulting/claude-memory/commit/34a5ca1156bb0f3fa42a72990c9d0c5f15638344))
* **tokens:** revocation truth, limiter scope, last_used commit, check order ([2912a77](https://github.com/first-it-consulting/claude-memory/commit/2912a772cda240f3ea97927e41556dafc0264cbc))
