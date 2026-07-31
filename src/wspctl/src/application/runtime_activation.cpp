#include "wspctl/application/runtime_activation.hpp"

#include <utility>

namespace wspctl::application {

RuntimeEstablishFailure::RuntimeEstablishFailure(
    const RuntimeEstablishFailureDisposition disposition, domain::Error cause) noexcept
    : disposition_(disposition), cause_(std::move(cause)) {}

RuntimeEstablishFailure RuntimeEstablishFailure::rejected_cleanly(domain::Error cause) {
    return RuntimeEstablishFailure(RuntimeEstablishFailureDisposition::rejected_cleanly,
                                   std::move(cause));
}

RuntimeEstablishFailure RuntimeEstablishFailure::cleanup_required(domain::Error cause) {
    return RuntimeEstablishFailure(RuntimeEstablishFailureDisposition::cleanup_required,
                                   std::move(cause));
}

RuntimeEstablishFailure RuntimeEstablishFailure::outcome_unknown(domain::Error cause) {
    return RuntimeEstablishFailure(RuntimeEstablishFailureDisposition::outcome_unknown,
                                   std::move(cause));
}

RuntimeEstablishFailureDisposition RuntimeEstablishFailure::disposition() const noexcept {
    return disposition_;
}

const domain::Error& RuntimeEstablishFailure::cause() const noexcept { return cause_; }

domain::Result<void> RuntimeActivationService::activate(domain::Runtime& runtime,
                                                        const domain::ActivationId& activation,
                                                        RuntimeActivationPort& port) const {
    if (const auto started = runtime.begin_activation(activation); !started) {
        return std::unexpected(started.error());
    }
    if (const auto established = port.establish(runtime.id(), activation); !established) {
        /** @brief 必须在 port 后续调用前保留的建立失败原因 / Establishment cause retained before
         * subsequent port calls. */
        const domain::Error cause = established.error().cause();
        switch (established.error().disposition()) {
        case RuntimeEstablishFailureDisposition::rejected_cleanly:
            if (const auto rejected = runtime.reject_activation(activation); !rejected) {
                return std::unexpected(rejected.error());
            }
            return std::unexpected(cause);
        case RuntimeEstablishFailureDisposition::cleanup_required:
            if (const auto stopped = stop(runtime, activation, port); !stopped) {
                return std::unexpected(stopped.error());
            }
            return std::unexpected(cause);
        case RuntimeEstablishFailureDisposition::outcome_unknown:
            if (const auto quarantined = runtime.quarantine_activation(activation); !quarantined) {
                return std::unexpected(quarantined.error());
            }
            return std::unexpected(cause);
        }
        return std::unexpected(
            domain::make_error(domain::ErrorCode::illegal_transition,
                               "runtime establish port returned an unknown failure disposition"));
    }
    if (const auto ready = runtime.mark_ready(activation); !ready) {
        if (const auto stopped = stop(runtime, activation, port); !stopped) {
            return std::unexpected(stopped.error());
        }
        return std::unexpected(ready.error());
    }
    return {};
}

domain::Result<void> RuntimeActivationService::stop(domain::Runtime& runtime,
                                                    const domain::ActivationId& activation,
                                                    RuntimeActivationPort& port) const {
    if (const auto stopping = runtime.begin_stop(activation); !stopping) {
        return std::unexpected(stopping.error());
    }
    if (const auto stopped = port.terminate(runtime.id(), activation); !stopped) {
        if (const auto recorded = runtime.record_stop_failure(activation); !recorded) {
            return std::unexpected(recorded.error());
        }
        return std::unexpected(stopped.error());
    }
    return runtime.finish_stop(activation);
}

} // namespace wspctl::application
