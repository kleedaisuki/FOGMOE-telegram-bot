#pragma once

#include "wspctl/infrastructure/common.hpp"

namespace wspctl::presentation {

/**
 * @brief 向 systemd 宣告当前主进程已经可处理请求 / Notify systemd that the main process is ready to serve requests.
 * @return 通知已可靠排入 systemd socket，或可诊断错误 / Notification queued to the systemd socket, or a diagnosable error.
 * @note 调用方必须只在全部 listener 与 worker 就绪后调用。函数会清除 `NOTIFY_SOCKET`，
 *       防止后续 runtime child 继承 service-manager capability。/
 *       The caller must invoke this only after every listener and worker is ready. The function
 *       clears `NOTIFY_SOCKET` so later runtime children cannot inherit the service-manager capability.
 */
[[nodiscard]] Result<void> notify_systemd_ready();

}  // namespace wspctl::presentation
