#include "wspctl/application/runtime_activation.hpp"

namespace wspctl::application {

domain::Result<void> RuntimeActivationService::activate(domain::Runtime& runtime,
                                                        const domain::ActivationId& activation,
                                                        RuntimeActivationPort& port) const {
    if (const auto started = runtime.begin_activation(activation); !started) {
        return std::unexpected(started.error());
    }
    if (const auto established = port.establish(runtime.id(), activation); !established) {
        runtime.fail();
        return std::unexpected(established.error());
    }
    if (const auto ready = runtime.mark_ready(activation); !ready) {
        const auto cleaned = port.retire(runtime.id(), activation);
        runtime.fail();
        if (!cleaned) {
            return std::unexpected(cleaned.error());
        }
        return std::unexpected(ready.error());
    }
    return {};
}

domain::Result<void> RuntimeActivationService::retire(domain::Runtime& runtime,
                                                      const domain::ActivationId& activation,
                                                      RuntimeActivationPort& port) const {
    if (const auto retiring = runtime.begin_retirement(activation); !retiring) {
        return std::unexpected(retiring.error());
    }
    if (const auto retired = port.retire(runtime.id(), activation); !retired) {
        runtime.fail();
        return std::unexpected(retired.error());
    }
    if (const auto dormant = runtime.finish_retirement(activation); !dormant) {
        runtime.fail();
        return std::unexpected(dormant.error());
    }
    return {};
}

domain::Result<void> RuntimeActivationService::abort(domain::Runtime& runtime,
                                                     const domain::ActivationId& activation,
                                                     RuntimeActivationPort& port) const {
    runtime.fail();
    if (const auto retired = port.retire(runtime.id(), activation); !retired) {
        return std::unexpected(retired.error());
    }
    return {};
}

} // namespace wspctl::application
