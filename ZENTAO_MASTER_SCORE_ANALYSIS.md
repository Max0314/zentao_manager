# Zentao Master AI 评分代码与数据库分析

本文档用于说明 `zentao_master` 中禅道 AI 评分的真实计算链路、正式库中的相关数据表，以及当前 `zentao_manager` 数据中转服务是否需要调整。

分析日期：2026-05-08  
代码目录：`D:\AI\zentao_master`  
中转服务目录：`D:\zentao_manager`  
正式库：`10.70.33.19:3380 / zentaoep`  

注意：本文档不记录数据库密码。生产环境请继续使用只读账号访问禅道库。

## 1. 结论先行

当前 `zentao_manager` 只同步 `zt_action` 操作日志：

```text
zt_action
  LEFT JOIN zt_user
  LEFT JOIN zt_dept
  -> POST /api/zentao/actions/batch
```

这个设计适合做“禅道操作流水同步”，也适合以后由 `bi_center` 自己按简单规则统计“创建了多少、完成了多少、解决了多少”。

但是，如果目标是获取下面这些数据：

- 每个员工每条任务、Bug、需求的 AI 得分
- 每条备注的 AI 得分
- 每个对象的字段明细分，例如任务名称分、任务描述分、Bug 标题分、Bug 复现步骤分
- 禅道“月度平均得分统计表”中的折算总分

那么当前代码需要调整。

原因是：AI 评分不在 `zt_action` 里，真实分数存储在 `zt_aiscore_result`。`zt_action` 只能告诉我们“发生了什么动作”，不能告诉我们“这条动作最终 AI 打了多少分”。

推荐调整方向：

1. 保留现有 `zt_action` 同步，用于操作流水。
2. 新增 `zt_aiscore_result` 增量同步，用于 AI 原始分、备注分、字段明细分。
3. 新增或定期快照同步 `zt_aiscore_rules`、`zt_ai_prompt`、`zt_config`，用于解释分数规则和月度折算配置。
4. 补充任务、Bug、需求维度信息，可通过 `zt_task`、`zt_bug`、`zt_story` 增量或快照同步实现。
5. `bi_center` 按 `zt_aiscore_result.id` 幂等入库，按业务口径计算报表。

## 2. 当前 zentao_manager 的能力边界

当前服务代码中，禅道查询逻辑在：

```text
D:\zentao_manager\app\zentao_client.py
```

核心方法是 `fetch_actions()`，只查以下字段：

```sql
SELECT
    a.id AS action_id,
    a.actor AS actor,
    a.action AS action,
    a.objectType AS object_type,
    a.objectID AS object_id,
    a.date AS action_date,
    a.extra AS extra,
    u.dept AS dept_id,
    d.name AS dept_name,
    u.deleted AS user_deleted
FROM zt_action a
LEFT JOIN zt_user u ON a.actor = u.account
LEFT JOIN zt_dept d ON u.dept = d.id
WHERE a.id > %s
  AND a.date >= %s
ORDER BY a.id ASC
LIMIT %s
```

本地状态表也只有 `last_action_id` 一个游标：

```text
D:\zentao_manager\app\state_store.py
sync_state.last_action_id
```

推送接口固定为：

```text
POST {BI_CENTER_URL}/api/zentao/actions/batch
```

当前设计里刻意不读取 `zt_action.comment`，这是对的，因为备注正文可能包含敏感信息。但这也意味着：当前服务无法让 `bi_center` 自己重新做 AI 备注评分。更合理的方式是同步已经计算好的备注分，也就是 `zt_aiscore_result.field = 'comment'` 的记录。

## 3. zentao_master 中 AI 评分的代码链路

评分链路可以理解为：

```text
用户在禅道操作任务/Bug/需求/备注
  -> action 扩展创建 zt_action
  -> 判断是否需要 AI 评分
  -> 写入 zt_aiscore_queue
  -> ai.asyncexecute 异步执行
  -> 调用大模型返回 JSON 分数
  -> saveAIScoreResult 保存到 zt_aiscore_result
  -> 普通对象评分还会回写 zt_task/zt_bug/zt_story.aiScore
```

### 3.1 动作触发评分队列

文件：

```text
D:\AI\zentao_master\zentao\changhong\extension\custom\action\ext\model\changhong.php
```

关键逻辑：

- 创建 action 后，先调用 `skipScoreAction()` 判断是否跳过评分。
- 如果是备注或评论动作，会取 `other.remarkscore` 类型的 Prompt。
- 如果是普通对象动作，会取 `other.score` 类型的 Prompt。
- 非纯数字备注会进入 AI 队列 `zt_aiscore_queue`。
- 纯数字备注会走 `genDigitalScore()`，直接给该条备注生成 0 分记录。

跳过的动作包括：

```text
story.submitreview
task.assigned
bug.assigned
story.assigned
story.linked2project
bug.converttotask
bug.tostory
```

另外，`story.edited` 且 `extra = storyChange` 会被跳过，避免需求编辑重复触发。

### 3.2 异步执行评分

文件：

```text
D:\AI\zentao_master\zentao\changhong\extension\custom\ai\ext\control\asyncexecute.php
```

核心逻辑：

1. 从 `zt_aiscore_queue` 读取 `status = 'wait'` 的任务。
2. 批量改成 `executing`。
3. 调用 `executePromptByModule()` 执行 AI Prompt。
4. 如果 AI 返回错误码，写入 `zt_action` 的 `aiscoredfail`，并记录 `zt_aiscore_failobject`。
5. 如果 AI 返回 JSON 字符串，调用 `saveAIScoreResult()` 保存分数。
6. 执行完成后删除 `status = 'executed'` 的队列记录。

需要关注的风险：正式库目前存在大量 `executing` 队列记录，说明异步评分任务可能有一批卡住或没有被清理。

### 3.3 Prompt 如何取数

基础 AI 模型代码在：

```text
D:\AI\zentao_master\zentao\changhongpatch\module\ai\model.php
```

重要函数：

- `getObjectForPromptById()`：按对象类型读取 `zt_task`、`zt_bug`、`zt_story` 等对象数据。
- `serializeDataToPrompt()`：把对象数据组装成给 AI 的 JSON 文本。
- `serializeCommentDataToPrompt()`：把备注组装成给 AI 的 JSON 文本。
- `genRuleFunctionCall()`：根据 Prompt 的 source 字段生成 AI 返回 JSON 的字段结构。
- `genCommentFunctionCall()`：生成单条备注评分返回结构。
- `executePrompt()`：调用大模型，返回 JSON 分数。

普通对象评分的数据源由 `zt_ai_prompt.source` 决定，例如任务评分 Prompt 使用：

```text
task.name
task.desc
task.remarkAll
```

Bug 评分 Prompt 使用：

```text
bug.title
bug.steps
bug.remarkAll
bug.remark
```

需求评分 Prompt 使用：

```text
story.title
story.spec
story.verify
story.source
story.sourceNote
```

### 3.4 分数如何保存

文件：

```text
D:\AI\zentao_master\zentao\changhong\extension\custom\aiscore\model.php
```

核心函数：`saveAIScoreResult()`

AI 返回 JSON 后，代码会：

1. 解析 JSON。
2. 取出 `AIsuggestions` 作为改进意见。
3. 逐个字段保存到 `zt_aiscore_result`。
4. 每个字段保存前都会调用 `calculateScore()` 校验分数范围。
5. 普通对象评分会再插入一条 `field = ''` 的总分记录。
6. 备注评分不会插入总分记录，只保存 `field = 'comment'`。
7. 普通对象评分会回写对象表的 `aiScore` 字段。

普通对象评分的总分计算：

```text
field='' 的 score = 本次 AI 返回的各字段分数之和
```

备注评分：

```text
field='comment'
action='commented'
actionID = 对应的 zt_action.id
score = 单条备注分
```

### 3.5 分数范围校验

函数：`calculateScore($field, $objectType, $score)`

逻辑：

1. 从 `zt_aiscore_rules` 读取该对象、该字段的 `scoreMin` 和 `scoreMax`。
2. 如果没有规则，原样返回 AI 分数。
3. 如果分数不是数字，或小于最低分，或大于最高分，改成区间中位数。

示例：

```text
字段范围 0 到 40
AI 返回 100
最终保存 round((0 + 40) / 2, 2) = 20
```

所以数据库中的 `zt_aiscore_result.score` 已经是经过禅道规则校验后的最终分数。

## 4. 正式库中的核心表

以下是 2026-05-08 查询正式库时的表数据规模。

| 表 | 行数 | 用途 |
|---|---:|---|
| `zt_action` | 1,763,579 | 禅道操作日志、备注、动作流水 |
| `zt_user` | 691 | 用户账号、姓名、部门 |
| `zt_dept` | 58 | 部门 |
| `zt_task` | 82,433 | 任务对象 |
| `zt_bug` | 59,001 | Bug 对象 |
| `zt_story` | 19,445 | 需求对象 |
| `zt_case` | 87,151 | 用例 |
| `zt_doc` | 5,765 | 文档 |
| `zt_aiscore_result` | 179,502 | AI 评分结果，最核心 |
| `zt_aiscore_rules` | 55 | 字段评分规则和分值范围 |
| `zt_aiscore_weight` | 15 | Prompt 字段权重记录，当前权重基本为 0 |
| `zt_aiscore_queue` | 4,959 | AI 评分队列 |
| `zt_aiscore_failobject` | 221 | AI 评分失败对象 |
| `zt_ai_prompt` | 14 | AI 提词配置 |
| `zt_config` | 12,211 | 系统配置，包含月度统计折算系数 |
| `zt_score` | 139,041 | 禅道系统内置积分，不是 AI 分 |

## 5. zt_aiscore_result 表解释

这是 BI 要拿 AI 分数时最重要的表。

| 字段 | 类型 | 通俗说明 |
|---|---|---|
| `id` | int | 评分结果主键，增量同步游标应该用它 |
| `objectType` | varchar | 对象类型，如 `task`、`bug`、`story`、`requirement` |
| `objectID` | int | 对象 ID，对应 `zt_task.id`、`zt_bug.id`、`zt_story.id` |
| `action` | varchar | 触发评分的动作，如 `opened`、`finished`、`resolved`、`commented` |
| `actionID` | int | 备注评分时对应 `zt_action.id`；普通对象评分通常为 0 |
| `field` | varchar | 评分字段；空字符串代表本次对象总分 |
| `score` | varchar | 分数，虽然是字符串，但内容通常是数字 |
| `suggestions` | longtext | AI 改进意见，通常只有 `field=''` 的总分行有值 |
| `times` | int | 同一个对象第几次评分 |
| `createBy` | varchar | 评分归属人，也是统计到哪个员工头上的关键字段 |
| `createDate` | datetime | 评分生成时间，也是月度归属的重要时间 |

最重要的口径：

```sql
-- 真实可计分行：对象总分 + 单条备注分
WHERE field = '' OR field = 'comment'
```

不要把下面这些字段和 `field=''` 直接相加：

```text
name
title
desc
steps
remark
remarkAll
spec
verify
source
sourceNote
```

这些是明细字段分，已经被 `field=''` 汇总过。如果同时加，会重复计算。

## 6. zt_aiscore_rules 表解释

`zt_aiscore_rules` 定义每个对象字段的评分规则和分数范围。

| 字段 | 通俗说明 |
|---|---|
| `objectType` | 对象类型，如 `task`、`bug`、`story` |
| `field` | 字段名，如 `name`、`desc`、`title`、`steps` |
| `rules` | 给 AI 的评分标准说明 |
| `scoreMin` | 最低分 |
| `scoreMax` | 最高分 |
| `editDate` | 最近编辑时间 |

正式库里比较关键的规则：

| 对象 | 字段 | 分数范围 | 含义 |
|---|---|---:|---|
| `task` | `name` | 0-20 | 任务名称是否清晰 |
| `task` | `desc` | 0-40 | 任务描述是否完整 |
| `task` | `totalRemark` / `remarkAll` | 0-40 | 解决过程、备注是否完整 |
| `task` | `singleRemarkForComment` | 0-5 | 单条备注是否有意义 |
| `bug` | `title` | 0-20 | Bug 标题是否清晰 |
| `bug` | `steps` | 0-40 | 复现步骤是否完整 |
| `bug` | `totalRemark` / `remarkAll` | 0-40 | 解决方案或备注是否完整 |
| `bug` | `singleRemarkForComment` | 0-5 | 单条备注是否有意义 |
| `story` | `title` | 0-15 | 需求标题 |
| `story` | `spec` | 0-50 | 需求描述 |
| `story` | `verify` | 0-25 | 验收标准 |
| `story` | `source` | 0-5 | 需求来源 |
| `story` | `sourceNote` | 0-5 | 来源备注 |
| `story` | `totalRemark` | 0-10 | 需求整体备注 |
| `story` | `singleRemarkForComment` | 0-5 | 单条备注 |

有些字段规则是“此项不计入 AI 评分”，分数范围是 0-0，例如状态、优先级、执行、预估工时等。

## 7. zt_ai_prompt 表解释

`zt_ai_prompt` 决定：

1. 哪些动作触发评分。
2. 给 AI 看哪些字段。
3. AI 返回什么形式的结果。

正式库中与评分直接相关的活动 Prompt：

| id | 名称 | module | targetForm | triggerControl | source |
|---:|---|---|---|---|---|
| 9 | 禅道需求 AI评分提词 | `story` | `other.score` | `requirement.create,requirement.edit,requirement.change` | `story.title,story.spec,story.verify,story.source,story.sourceNote` |
| 10 | 禅道任务 AI评审提词 | `task` | `other.score` | `task.create,task.edit,task.start,task.finish` | `task.name,task.desc,task.remarkAll` |
| 11 | 禅道BUG AI评分提词 | `bug` | `other.score` | `bug.create,bug.edit,bug.confirm,bug.resolve` | `bug.title,bug.steps,bug.remarkAll,bug.remark` |
| 12 | 需求备注（单条）AI评审提词 | `story` | `other.remarkscore` | 空 | `story.remark` |
| 13 | 任务备注（单条）AI评审提词 | `task` | `other.remarkscore` | 空 | `task.remark` |
| 14 | Bug备注（单条）AI评审提词 | `bug` | `other.remarkscore` | 空 | `bug.remark` |

还有一些系统自带 Prompt，例如“需求润色”“任务润色”“Bug润色”，它们不等同于 AI 评分统计。

## 8. 月度统计折算逻辑

禅道报表中的“月度平均得分统计表”不只是把 `zt_aiscore_result.score` 简单求和，而是二次折算。

代码位置：

```text
D:\AI\zentao_master\zentao\changhong\extension\custom\pivot\ext\model\changhong.php
```

核心入口：

```text
aiScoreAvgByMonth($begin, $end, $dept = 0, $user = '', $pager = null)
```

正式库当前折算系数来自 `zt_config`：

| 指标 | 配置值 |
|---|---:|
| `bugCreate` | 15 |
| `bugResolve` | 5 |
| `taskCreate` | 5 |
| `taskFinish` | 5 |
| `storyCreate` | 2 |
| `storyChange` | 2 |
| `testcaseCreate` | 2 |
| `docCreate` | 2 |

等级配置：

| 等级 | 排名百分比 |
|---|---|
| S | 0-3% |
| A | 3-10% |
| B | 10-60% |
| C | 60-90% |
| D | 90-100% |

### 8.1 Bug 创建分

代码逻辑：

```text
找到本月 openedBy = 用户 的 Bug
取这些 Bug 在 zt_aiscore_result 中 action='opened' 的 title、steps 分
计算 title 平均分和 steps 平均分
Bug 创建分 = Bug 创建数量 * round((titleAvg + stepsAvg) / 60, 2) * bugCreate配置
```

注意：这里用的是 `zt_bug.openedBy/openedDate` 定义“创建数量”，但用 `zt_aiscore_result` 的字段分定义质量。

### 8.2 Bug 备注分

代码逻辑：

```text
Bug 备注分 = 本月 createBy = 用户 且 objectType='bug' 且 field='comment' 的备注分之和
```

备注分来自 `zt_aiscore_result`，不是来自 `zt_action.comment` 文本。

### 8.3 Bug 解决分

代码逻辑：

```text
找到本月 createBy = 用户 的 Bug 总评分记录 field=''
统计其中 action='resolved' 的数量
计算这些 Bug 总评分记录的平均分
Bug 解决分 = resolved数量 * 平均分 * bugResolve配置 / 100
```

### 8.4 任务创建分

代码逻辑：

```text
找到本月 openedBy = 用户 的任务
取这些任务在 zt_aiscore_result 中 action='opened' 的 name、desc 分
计算 name 平均分和 desc 平均分
任务创建分 = 任务创建数量 * round((nameAvg + descAvg) / 60, 2) * taskCreate配置
```

### 8.5 任务备注分

代码逻辑：

```text
任务备注分 = 本月 createBy = 用户 且 objectType='task' 且 field='comment' 的备注分之和
```

### 8.6 任务完成分

代码逻辑：

```text
找到本月 createBy = 用户 的任务总评分记录 field=''
统计其中 action='finished' 的数量
计算这些任务总评分记录的平均分
任务完成分 = finished数量 * 平均分 * taskFinish配置 / 100
```

### 8.7 需求创建分与需求变更分

代码逻辑：

```text
找到本月 createBy = 用户 且 objectType in ('story', 'requirement') 且 field='' 的总评分记录
action='opened' 或 action='frombug' 计入需求创建数量
action='changed' 计入需求变更数量
需求创建分 = 创建数量 * 平均分 * storyCreate配置 / 100
需求变更分 = 变更数量 * 平均分 * storyChange配置 / 100
```

正式库中目前观察到 `story` 基本没有 `field=''` 的总分记录，因此当前月度报表里的需求 AI 折算分可能为 0。虽然 `zt_story` 中能看到用户创建和评审了需求，但没有 AI 总分就无法按这套公式产生需求分。

### 8.8 用例与原创文档

用例创建分：

```text
testcaseCreate = 本月 zt_case.openedBy = 用户 的数量 * testcaseCreate配置
```

原创文档创建分：

```text
docCreate = 本月 zt_doc.addedBy = 用户 且 title 包含“原创”的数量 * docCreate配置
```

### 8.9 总分

代码最后直接累加：

```text
totalScore =
  bugCreate
  + bugRemark
  + bugResolve
  + taskCreate
  + taskRemark
  + taskFinish
  + storyCreate
  + storyChange
  + testcaseCreate
  + docCreate
```

## 9. 正式库评分数据分布

正式库当前 `zt_aiscore_result` 中，真实可计分行建议按：

```sql
WHERE field = '' OR field = 'comment'
```

主要分布如下：

| 对象 | action | field | 行数 | 分数合计 |
|---|---|---|---:|---:|
| bug | `opened` | 空 | 7,308 | 430,540 |
| bug | `resolved` | 空 | 4,742 | 351,315.5 |
| bug | `bugconfirmed` | 空 | 2,378 | 165,006 |
| bug | `edited` | 空 | 2,562 | 188,027.5 |
| bug | `commented` | `comment` | 13,050 | 35,558.5 |
| task | `opened` | 空 | 2,365 | 76,575.5 |
| task | `started` | 空 | 2,497 | 89,635.5 |
| task | `finished` | 空 | 6,409 | 259,988.5 |
| task | `edited` | 空 | 1,984 | 89,231 |
| task | `commented` | `comment` | 12,702 | 38,635 |
| story | `commented` | `comment` | 204 | 579 |

字段明细分布中，任务和 Bug 主要是：

| 对象 | 字段 | 行数 | 分数合计 |
|---|---|---:|---:|
| bug | `title` | 16,971 | 286,921 |
| bug | `steps` | 16,974 | 574,486 |
| bug | `remarkAll` | 16,974 | 82,523 |
| bug | `remark` | 16,974 | 75,453.5 |
| task | `name` | 14,615 | 219,923.5 |
| task | `desc` | 14,615 | 232,397 |
| task | `remarkAll` | 14,280 | 101,857 |

再次强调：字段明细分不能和 `field=''` 总分重复相加。

## 10. 当前正式库的异常点

### 10.1 AI 队列存在大量 executing

正式库 `zt_aiscore_queue`：

| status | 行数 | 最早创建 | 最新创建 |
|---|---:|---|---|
| `executing` | 4,957 | 2026-03-15 12:27:58 | 2026-05-08 14:04:36 |
| `wait` | 2 | 2026-05-08 15:07:05 | 2026-05-08 15:07:05 |

按照代码，正常执行完成后队列会变成 `executed` 并被删除。如果长期存在大量 `executing`，可能表示：

- 异步任务进程中途异常退出，没有把状态恢复；
- `ai.asyncexecute` 定时调用不稳定；
- 大模型接口返回慢或失败；
- 代码只处理 `wait`，不会自动重试旧的 `executing`。

这会影响后续评分是否持续生成。

### 10.2 需求总分缺失

正式库中：

- `zt_story` 有 19,445 行。
- `zt_story.aiScore` 非 0 行数为 0。
- `zt_aiscore_result` 中 `story` 主要只有 `field='comment'` 的备注分。

这说明需求的 AI 总分链路可能没有正常落库，或者当前业务只启用了需求备注评分，没有产生需求对象总分。

如果 BI 要统计需求创建分、需求变更分，需要重点确认：

```sql
SELECT objectType, action, field, COUNT(*)
FROM zt_aiscore_result
WHERE objectType IN ('story', 'requirement')
GROUP BY objectType, action, field;
```

如果没有 `field=''`，按当前禅道月报公式，需求相关折算分会是 0。

## 11. 陈鹏列 2026 年 4 月样例核对

用户：

```text
account = chenpenglie
realname = 陈鹏列
dept = 应用开发组
scoreStatistic = 1
```

### 11.1 AI 原始实际分

按 `zt_aiscore_result` 中：

```sql
field = '' OR field = 'comment'
```

统计 2026-04-01 到 2026-04-30：

| 对象 | action | field | 行数 | 分数 |
|---|---|---|---:|---:|
| bug | `commented` | `comment` | 9 | 28 |
| bug | `resolved` | 空 | 1 | 58 |
| task | `commented` | `comment` | 39 | 188 |
| task | `edited` | 空 | 2 | 272.5 |
| task | `finished` | 空 | 8 | 1,079.5 |
| task | `opened` | 空 | 9 | 1,217.5 |
| task | `started` | 空 | 1 | 77 |

原始可计分合计：

```text
2920.5
```

这个口径是“AI 原始得分”，适合展示每条对象、每条备注的实际 AI 分数。

### 11.2 禅道月度折算分

按 `pivot/ext/model/changhong.php` 的月度统计公式复算：

| 指标 | 分数 |
|---|---:|
| Bug 创建分 | 0 |
| Bug 备注分 | 14 |
| Bug 解决分 | 2.9 |
| 任务创建分 | 47.5 |
| 任务备注分 | 188 |
| 任务完成分 | 52.93 |
| 需求创建分 | 0 |
| 需求变更分 | 0 |
| 用例创建分 | 0 |
| 原创文档创建分 | 0 |
| 总分 | 305.33 |

这个口径是“禅道月度平均得分统计表”的折算结果，适合做排行榜、等级。

两个结果差异很大是正常的，因为它们不是同一个口径：

```text
AI 原始分：2920.5
月度折算分：305.33
```

## 12. bi_center 应该采用哪个口径

建议同时保留两层数据：

### 12.1 明细层：AI 原始评分明细

用于回答：

- 某个人某条任务得了多少分？
- 某条 Bug 的标题、步骤、备注分别多少分？
- 某条备注得了多少分？
- AI 给出的改进意见是什么？

来源表：

```text
zt_aiscore_result
zt_task
zt_bug
zt_story
zt_action
zt_user
zt_dept
```

### 12.2 指标层：月度折算分

用于回答：

- 某个人 4 月份总分多少？
- 任务创建分多少？
- 任务完成分多少？
- Bug 解决分多少？
- 排名等级是什么？

来源表：

```text
zt_aiscore_result
zt_task
zt_bug
zt_story
zt_case
zt_doc
zt_user
zt_dept
zt_config
```

## 13. 对 zentao_manager 的改造建议

### 13.0 当前工程已经完成的改造

本工程已从单一 `actions` 数据流扩展为多数据流同步。默认启用：

```text
actions,aiscore_results,tasks,bugs,stories,users,depts,aiscore_rules,ai_prompts,pivot_configs,cases,docs
```

对应推送接口：

| 数据流 | 推送接口 | 游标字段 | 同步方式 |
|---|---|---|---|
| `actions` | `/api/zentao/actions/batch` | `action_id` | 按 `zt_action.id` 增量 |
| `aiscore_results` | `/api/zentao/aiscore-results/batch` | `score_result_id` | 按 `zt_aiscore_result.id` 增量 |
| `tasks` | `/api/zentao/tasks/batch` | `task_id` | 按 ID 循环快照 |
| `bugs` | `/api/zentao/bugs/batch` | `bug_id` | 按 ID 循环快照 |
| `stories` | `/api/zentao/stories/batch` | `story_id` | 按 ID 循环快照 |
| `users` | `/api/zentao/users/batch` | `user_id` | 按 ID 循环快照 |
| `depts` | `/api/zentao/depts/batch` | `dept_id` | 按 ID 循环快照 |
| `aiscore_rules` | `/api/zentao/aiscore-rules/batch` | `rule_id` | 按 ID 循环快照 |
| `ai_prompts` | `/api/zentao/ai-prompts/batch` | `prompt_id` | 按 ID 循环快照 |
| `pivot_configs` | `/api/zentao/pivot-configs/batch` | `config_id` | 按 ID 循环快照，仅同步 AI 月报相关配置 |
| `cases` | `/api/zentao/cases/batch` | `case_id` | 按 ID 循环快照 |
| `docs` | `/api/zentao/docs/batch` | `doc_id` | 按 ID 循环快照 |

事件流使用 `INITIAL_SYNC_START` 控制首次补数边界；快照流扫到表尾后会自动把该流游标重置为 0，下一轮重新从头快照，以捕获任务状态、Bug 状态、用户部门等原地更新。

### 13.1 不改代码也可以的情况

如果 `bi_center` 只需要：

- 禅道操作流水；
- 用户做了哪些操作；
- 根据 `opened`、`finished`、`resolved` 这些 action 自己算简单积分；

那么当前 `zentao_manager` 不需要改。

但这不是禅道 AI 积分的真实口径。

### 13.2 必须改代码的情况

如果 `bi_center` 需要：

- AI 真实分数；
- 每条任务、Bug、需求的分数；
- 每条备注分数；
- 字段明细分；
- 还原禅道月度报表；

那么必须新增评分数据同步。

### 13.3 推荐新增数据流

#### 数据流 1：操作流水

保留现有：

```text
POST /api/zentao/actions/batch
```

幂等键：

```text
action_id = zt_action.id
```

#### 数据流 2：AI 评分结果

新增：

```text
POST /api/zentao/aiscore-results/batch
```

来源：

```sql
SELECT
    r.id AS score_result_id,
    r.objectType AS object_type,
    r.objectID AS object_id,
    r.action AS action,
    r.actionID AS action_id,
    r.field AS field,
    r.score AS score,
    r.suggestions AS suggestions,
    r.times AS score_times,
    r.createBy AS create_by,
    r.createDate AS create_date,
    u.dept AS dept_id,
    d.name AS dept_name,
    u.realname AS realname,
    u.deleted AS user_deleted
FROM zt_aiscore_result r
LEFT JOIN zt_user u ON r.createBy = u.account
LEFT JOIN zt_dept d ON u.dept = d.id
WHERE r.id > ?
ORDER BY r.id ASC
LIMIT ?
```

幂等键：

```text
score_result_id = zt_aiscore_result.id
```

建议载荷：

```json
{
  "source": "zentao",
  "database": "zentaoep",
  "batch_id": "uuid",
  "sent_at": "2026-05-08T16:00:00+08:00",
  "items": [
    {
      "score_result_id": 179502,
      "object_type": "task",
      "object_id": 81799,
      "action": "finished",
      "action_id": 0,
      "field": "",
      "score": "25",
      "suggestions": "任务名称较为宽泛...",
      "score_times": 3,
      "create_by": "jiangxiaoqing",
      "create_date": "2026-05-08T15:04:32",
      "dept_id": 12,
      "dept_name": "应用开发组",
      "realname": "姓名",
      "user_deleted": "0"
    }
  ]
}
```

#### 数据流 3：对象维度表

为了在 BI 中展示标题、状态、创建人、完成人、解决人，需要同步对象维度。

可以做成每日全量快照，或按更新时间增量同步。

建议先用每日快照，简单稳定。

任务维度：

```sql
SELECT
    id,
    name,
    project,
    execution,
    status,
    openedBy,
    openedDate,
    assignedTo,
    finishedBy,
    finishedDate,
    deleted,
    aiScore
FROM zt_task;
```

Bug 维度：

```sql
SELECT
    id,
    title,
    project,
    product,
    execution,
    status,
    openedBy,
    openedDate,
    assignedTo,
    resolvedBy,
    resolvedDate,
    deleted,
    aiScore
FROM zt_bug;
```

需求维度：

```sql
SELECT
    id,
    title,
    product,
    type,
    status,
    openedBy,
    openedDate,
    assignedTo,
    reviewedBy,
    reviewedDate,
    deleted,
    aiScore
FROM zt_story;
```

#### 数据流 4：评分规则与配置

低频同步即可，例如每天一次。

```text
zt_aiscore_rules
zt_ai_prompt
zt_config 中 owner='system' AND module='pivot' 的评分配置
```

这些表用于解释：

- 为什么某字段满分是 20 或 40；
- 当前月度折算系数是多少；
- 当前等级分布是多少；
- 哪些动作会触发评分。

### 13.4 SQLite 状态表需要改造

当前只有：

```text
sync_state.last_action_id
```

如果新增多条数据流，建议改成通用游标表：

```sql
CREATE TABLE sync_cursors (
  stream_name TEXT PRIMARY KEY,
  last_id INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL
);
```

示例游标：

| stream_name | last_id 含义 |
---|---|
| `actions` | `zt_action.id` |
| `aiscore_results` | `zt_aiscore_result.id` |
| `task_snapshot` | 快照不用 id，可记录时间 |
| `bug_snapshot` | 快照不用 id，可记录时间 |
| `story_snapshot` | 快照不用 id，可记录时间 |

### 13.5 bi_center 入库建议

至少建以下表：

```text
zentao_action_event
zentao_ai_score_result
zentao_task_dim
zentao_bug_dim
zentao_story_dim
zentao_user_dim
zentao_dept_dim
zentao_ai_score_rule
zentao_ai_prompt
zentao_pivot_score_config
```

其中最核心的是：

```text
zentao_ai_score_result
```

字段建议与 `zt_aiscore_result` 一一对应，并额外保存 `batch_id`、`received_at`。

## 14. BI 查询口径建议

### 14.1 查询每个人 AI 原始实际分

```sql
SELECT
    create_by,
    SUM(CAST(score AS DECIMAL(12, 2))) AS actual_ai_score
FROM zentao_ai_score_result
WHERE create_date >= '2026-04-01 00:00:00'
  AND create_date <  '2026-05-01 00:00:00'
  AND field IN ('', 'comment')
GROUP BY create_by;
```

### 14.2 查询每个人备注分

```sql
SELECT
    create_by,
    object_type,
    COUNT(*) AS comment_count,
    SUM(CAST(score AS DECIMAL(12, 2))) AS comment_score
FROM zentao_ai_score_result
WHERE create_date >= '2026-04-01 00:00:00'
  AND create_date <  '2026-05-01 00:00:00'
  AND field = 'comment'
GROUP BY create_by, object_type;
```

### 14.3 查询每条任务的最近总分

```sql
SELECT r.*
FROM zentao_ai_score_result r
JOIN (
    SELECT object_type, object_id, MAX(id) AS max_id
    FROM zentao_ai_score_result
    WHERE object_type = 'task'
      AND field = ''
    GROUP BY object_type, object_id
) latest ON latest.max_id = r.id;
```

### 14.4 查询每条任务字段明细分

```sql
SELECT
    object_id,
    action,
    times,
    MAX(CASE WHEN field = 'name' THEN score END) AS name_score,
    MAX(CASE WHEN field = 'desc' THEN score END) AS desc_score,
    MAX(CASE WHEN field = 'remarkAll' THEN score END) AS remark_all_score,
    MAX(CASE WHEN field = '' THEN score END) AS total_score
FROM zentao_ai_score_result
WHERE object_type = 'task'
GROUP BY object_id, action, times;
```

## 15. 最终建议

`zentao_manager` 建议进入第二阶段改造。

第一阶段当前版本已经证明：

- 能连接禅道正式库；
- 能按游标增量读取；
- 能向 `bi_center` 推送；
- 有 outbox 失败重试机制。

第二阶段应新增：

1. `zt_aiscore_result` 增量同步。
2. 多游标状态管理。
3. 评分结果批量推送接口。
4. 任务、Bug、需求维度快照。
5. 评分规则、Prompt、月度配置快照。
6. 队列异常监控，例如统计 `zt_aiscore_queue` 中长期 `executing` 的数量。

这样 `bi_center` 才能既保留原始事件流水，又能准确还原禅道 AI 分数、备注分和月度排行榜。
