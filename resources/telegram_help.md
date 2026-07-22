*指令列表：*
/start - 开始使用
/help - 查看帮助文档
/ref - 查看邀请信息或绑定邀请人
/tl - 中英互译功能
/music - 搜索音乐

*账户、银行与权益（仅私聊）：*
/me - 注册或查看个人信息
/bank - 查看 Free 免费金币与 Paid legacy 历史余额
/request\_tokens <数量> <用途> - 申请免费金币，等待银行审核
/recharge <数量> <用途> - 与 /request\_tokens 相同；仅为申请简写，不是充值
/billing - 查看有效权益与订阅
/billing\_order <报价ID> [续费订阅ID] - 创建待付款权益/订阅订单
/refund <订单ID> <原因> - 发起退款申请
/subscription\_cancel <订阅ID> - 在本期结束时取消订阅

*博彩活动：*
/chance <规则> <免费金币押注> - 创建仅使用免费金币的承诺轮次；常规规则为 big/small/odd/even（大/小/单/双），高方差规则为 any-triple（豹子/围骰）及 triple-1 至 triple-6；会显示精确负 EV
/chance\_seed <轮次UUID> <客户端种子> - 提交客户端种子并结算
/chance\_show <轮次UUID> - 查询承诺、规则集与公平性证明

*金币互动：*
/lottery - 每日一次免费金币奖励
/checkin - 每日签到获得免费金币
/task - 查看并完成可用任务
/give <用户名> <数量> - 赠送免费金币
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
