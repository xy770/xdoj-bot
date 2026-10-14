XDOJ 自动答题机器人

基于 DeepSeek 的 XDOJ 自动答题工具。定时登录、抓取题目、生成 C 代码、本地验证、提交判题，全流程自动化。

功能

· 按时间窗口自动调度，每小时完成指定数量的 AC
· 每题仅提交一次，失败后进入 24 小时冷却
· 提交前本地编译并运行样例，避免无效提交
· 自动识别无效题目（题面过短或缺少关键词）
· 状态持久化，支持断点续跑

环境要求

· Python 3.9+
· g++（作为 C 编译器使用）
· DeepSeek API Key

安装

```bash
pip install requests openai python-dotenv pypdf
```

pypdf 不可用时会自动回退到 PyPDF2。

配置

在项目根目录创建 .env 文件：

```ini
# XDOJ 账号
XDOJ_BASE_URL=http://47.92.139.74/xdoj-ssm
XDOJ_CONTEST_ID=11
XDOJ_USER_ID=your_id
XDOJ_PASSWORD=your_password
XDOJ_CLASS_ID=your_class_id
XDOJ_LANGUAGE_ID=2

# DeepSeek
DEEPSEEK_API_KEY=sk-xxxxxx
DEEPSEEK_MODEL=deepseek-chat

# 调度
START_HOUR=8
END_HOUR=20
PROBLEMS_PER_HOUR=2
MAX_ATTEMPTS=6
COOLDOWN_HOURS=24
```

XDOJ_USER_ID、XDOJ_PASSWORD、XDOJ_CLASS_ID、DEEPSEEK_API_KEY 为必填项，其余参数均有默认值。

使用

```bash
# 启动调度器，按时间表运行
python main.py

# 执行一轮后退出
python main.py --once

# 查看题目列表
python main.py --list

# 处理指定题目
python main.py --problem-id 1234
```

工作流程

1. 登录并选择比赛
2. 拉取题目列表，过滤已完成、已跳过及冷却中的题目
3. 逐题处理：
   · 抓取题面文本或 PDF
   · 调用 DeepSeek 生成 C 代码及样例
   · 本地编译并运行样例，未通过则丢弃
   · 提交至 OJ 并轮询判题结果
4. 达成小时目标或尝试次数耗尽后结束本轮
5. 保存状态

目录结构

```
work/
├── problems/           # 题面文本与 PDF
├── code/
│   └── problem{id}/
│       ├── ai_response.json   # AI 原始返回
│       └── accepted.c         # 通过后的代码
└── state.json          # 运行状态
```

state.json 用于断点续跑，请勿手动删除。

注意事项

· 登录依赖 JSESSIONID，通过同一 Session 完成登录与选赛
· 扫描版或特殊字体的 PDF 可能无法提取文本，将被判定为无效题目
· 判题轮询通过 problemId 匹配最新记录，建议 PROBLEMS_PER_HOUR 不宜过大
· 仅在 START_HOUR 至 END_HOUR 时间窗口内执行任务

免责声明

本项目仅供学习与技术研究使用。请遵守平台的使用条款，因使用本工具产生的任何后果由使用者自行承担。