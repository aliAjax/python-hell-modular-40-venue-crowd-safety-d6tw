# 大型场馆人群安全与现场指挥

只使用Python标准库和SQLite的模块化服务，默认端口`8340`。支持场馆区域、入场口、容量、通道、安保岗位、医疗点、事件、限流、开放通道、疏散处置单和医疗任务、人员到位、区域恢复、延迟重复事件和容量冲突。

## 模块结构

- `app.py`：参数解析、依赖组装和服务生命周期。
- `src/domain.py`：角色、领域异常和数据对象。
- `src/rules.py`：容量计算、事件优先级、状态机和团队冲突约束。
- `src/repository.py`：SQLite持久化、UnitOfWork事务（`BEGIN IMMEDIATE`串行化）、乐观锁、幂等和审计查询。
- `src/evacuation.py`：疏散处置单编排（发起、结案恢复、旧数据升级）。
- `src/service.py`：用例编排、权限校验和版本控制。
- `src/http_api.py`：JSON接口和统一错误响应。
- `src/audit.py`：操作审计。
- `static/index.html`：最小演示页面。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8340
```

## 核心对象

`venue`为场馆，`zone`为区域，`gate`为入场口，`post`为安保岗位，`medical_point`为医疗点，`incident`为事件，`task`为现场任务，`evacuation_order`为疏散处置单。

## 疏散处置单

处置单把区域、事件、连通入场口和该区域内在途任务接成同一条命令（`command_no`命令号）。发起与结案均在单个SQLite串行化事务内执行，任一步失败整单回滚不生效，可用同一命令号或`Idempotency-Key`重试：

- 发起（`supervisor`/`coordinator`/`admin`）：区域进入`evacuating`，所有连通入场口同事务关闭并登记`evacuation_holds`命令号（记录疏散前状态），区域内`assigned/enroute/on_scene`任务退回`in_review`复核。疏散只能通过处置单进行，不再开放`zone.evacuate`/`zone.recover`通用动作。
- 命令号幂等：同命令号重提（含结案后）直接拿回原单；命令号绑定其他区域/事件返回`409`；同区域重复发单返回`409`并给出在执行的命令号。
- 后到放行：疏散期间从任一连通入口（含另一入口）提交放行，返回`409 ConflictError`且错误信息带冲突命令号；人工重开入口同样被拒。
- 结案恢复：必须事件已`resolved`且区域内无在途任务；共享入口按最后一个hold解除重开（恢复到疏散前状态，疏散前关闭的保持关闭）；operator越权恢复返回`403`。
- 旧数据升级：服务启动时按`PRAGMA user_version`迁移，存量`evacuating`区域自动补处置单、对齐入口（关闭+hold）、退回在途任务；缺事件时补建合成事件，迁移只执行一次且幂等。

## 接口

- `GET /health`
- `GET /api/<kind>`，可用`?status=`过滤
- `GET /api/entities/<id>`
- `POST /api/<kind>`
- `POST /api/entities/<id>/actions`
- `POST /api/evacuation_orders/issue`：发起处置单，body含`zone_id`、`incident_id`、`command_no`、`reason`，支持`Idempotency-Key`
- `POST /api/evacuation_orders/<id>/complete`：结案恢复，body可含`checklist`、`expected_version`
- `GET /api/audit`

身份通过`X-User-Id`和`X-Role`请求头传入。可选`Idempotency-Key`防止重复创建。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

容量和调度规则为可运行的简化模型，不接入闸机、视频分析、室内定位、消防联动或真实应急指挥系统。
