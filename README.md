# AI 新闻公众号草稿工作流

一个以微信公众号草稿为目标的 AI 新闻与开源项目编辑工作流。代码提供选题评分、来源快照、事实核验、图文检查、草稿保存、续跑和审计工具。草稿与公开发表是不同状态；项目默认不公开发表。

## 当前范围

- 先研究和评分近期 AI 新闻；没有合格新闻时，可按人工审阅的 GitHub 高星项目专题作为备用。
- 核对来源、榜单口径、数据和使用权限；文章、图片、人工目视和审批各自独立检查。
- 通过官方公众号草稿 API 保存；网络失败或结果不明时先查询已有草稿，不重复创建。
- 只读回验证标题、正文、图片及封面；自动检查不等于事实认证或视觉验收。

本仓库不包含任何已登录账号、密钥、草稿、文章、个人肖像、用户数据、定时任务或图片生成服务。榜单研究、文字创作、图片生成与实际视觉审核需要操作者和所选工具参与；它不是安装后可自动产生合格文章的独立服务。

## 安装

需要 Python 3.11 或更新版本。示例：

```bash
python -m venv .venv
python -m pip install -r requirements.txt
```

运行基础测试：

```bash
python -m pytest -q -p no:cacheprovider tests/test_topic_candidate_ranker.py tests/test_content_workflow_gate.py tests/test_wechat_draft_publisher.py tests/test_image_quality_gate.py
```

脱敏副本在制作环境中，上述核心测试为 73 项通过、8 个子测试通过。完整历史测试套件有 46 项失败，涉及被排除的个人历史材料、历史平台及专题配置等；因此该副本尚未通过全量回归，不应视为开箱即用的完整部署。需先补充通用测试夹具、修复兼容性，再连接真实账号。

## 工作流程

1. 收集和核查公开来源，将真实候选写入 `topic-candidates.json`。针对具体候选，按 `config/content-workflow-rubric.json` 填写评分、独立线索和实验；不要编造。
2. 通过 `scripts/huazi_content_ops.py pack --help` 查看发布包创建参数，通过 `scripts/topic_candidate_ranker.py --help` 查看评分命令。专题例外需要项目所有者自行填写、审核 `config/requested-topics.json` 和包内 `topic-request.json`，并绑定最终正文哈希；本仓库不预置任何例外。自动判断新闻不足并创建例外的功能尚未实现。
3. 生成和人工审核图文及来源记录后，用 `scripts/social_draft_router.py plan --help`、`scripts/research_evidence_gate.py --help`、`scripts/image_quality_gate.py --help`、`scripts/content_quality_gate.py --help` 执行检查。
4. 运行草稿路由的 `push --dry-run` 和工作流 `evaluate`；仅在所有必要检查通过且明确授权时，使用官方草稿接口。配置账号凭据需由使用者在本机安全完成，不要提交进仓库。
5. 保存后对同一草稿回读并检查图文与封面；保留实际证据。未知结果先核查，不重复创建。没有通过回读的状态不能称为完成。

发布包与恢复点均属于本地私有运行数据，不应提交。独立图像生成和视觉验收需要另外配置；原创角色参考与商用素材授权由使用者自行提供。实际微信可用性取决于账号权限、接口和网络环境，未公开发布验证。

## 安全与许可

请勿提交 API 密钥、Cookie、访问令牌、账号素材、后台截图、用户数据或本地发布包。本仓库尚未指定开源许可证；在明确许可之前，公开可见不等于授予复制、修改或分发的许可。项目参考的上游方法及其许可证见 `research/github-content-visual-review-2026-10-04.json`；未将其上游运行时作为本项目的一部分安装。
