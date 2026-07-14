*指令列表：*
/start - 开始使用
/help - 查看帮助文档
/ref - 查看邀请信息或绑定邀请人
/tl - 中英互译功能
/music - 搜索音乐

*账户、银行与权益（仅私聊）：*
/me - 注册或查看个人信息
/bank - 查看 Free 免费金币与 Paid legacy 历史余额
/request\_tokens <数量> <用途> - 申请免费金币；会可靠提醒银行管理员，等待审核
/recharge <数量> <用途> - 与 /request\_tokens 相同；仅为申请简写，不是充值
/billing - 查看有效权益与订阅
/billing\_order <报价ID> [续费订阅ID] - 创建待付款权益/订阅订单
/refund <订单ID> <原因> - 发起退款申请
/subscription\_cancel <订阅ID> - 在本期结束时取消订阅

*个人冒险（仅私聊）：*
/adventure - 查看个人角色、材料与每日进度
/adventure\_create <名称> - 创建角色
/adventure\_explore <woodland|quarry|shore> - 每日探索林地、采石场或海岸
/adventure\_craft <配方> - 制作 herbal\_lantern、rune\_charm 或 tidal\_mobile
/adventure\_collection - 查看收藏图鉴

*群组小镇（仅群聊或超级群）：*
/town - 创建或查看当前群的小镇
/town overview - 查看项目与群金库概览
/town project <hall|workshop|garden|observatory> <金币> <项目名> - 群管理员创建项目
/town contribute <免费金币> [项目ID] - 群成员贡献 Free 免费金币
/town complete <项目ID> - 群管理员建成已满足条件的项目

*可验证随机活动（私聊、群聊或超级群）：*
/chance <规则> <免费金币押注> - 创建只使用 Free 的承诺轮次；常规规则为 big/small/odd/even（大/小/单/双），高方差规则为 any-triple（豹子/围骰）及 triple-1 至 triple-6；会显示精确负 EV
/chance\_seed <轮次UUID> <客户端种子> - 提交客户端种子并结算
/chance\_show <轮次UUID> - 查询承诺、规则集与公平性证明

*Free 金币互动：*
/lottery - 每日一次免费金币奖励
/checkin - 每日签到获得 Free 免费金币
/task - 查看并完成可用任务
/give <用户名> <数量> - 赠送 Free 免费金币
/rich - 查看富豪榜前五

*群组相关：*
/fogmoebot - 在群组中连接
/report - 举报垃圾消息给群管理
/verify - 管理新成员验证
/spam - 垃圾消息管制
/keyword - 设置关键词自动回复
/chart - 代币图表功能
/resetgroup - 清空当前群聊的共享记忆（仅群管理员）

*聊天相关：*
/setmyinfo - 设置个性化提示词
/clear - 清空上下文并开始新对话
/resetmem - 清空个人记忆
/resetprofile - 清除 User Profile
/regen - 手动请求更新 User Profile

*其他娱乐与工具：*
/omikuji - 抽取御神签预测运势
/pic - 获取随机图片
/webpassword - 设置 Web 登录密码

*重要边界：*
支付只能在受控原生渠道验证成功后授予权益或订阅，绝不兑换为金币；用户不能通过命令声明已付款。系统没有卡密充值、$FOGMOE 买入/兑换/swap。Paid legacy 历史余额与 Free 隔离，不能用于随机活动或群组贡献。Telegram Stars 是否可用取决于具体部署的受控渠道配置，并非本帮助自动启用。

系统只执行本帮助中声明的命令。任何未声明命令都会统一提示使用 `/help`，绝不回落到历史逻辑；随机活动请使用 `/chance`。
