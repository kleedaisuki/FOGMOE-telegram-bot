#include "wspctl/presentation/unix_gateway.hpp"

#include "wspctl/domain/runtime.hpp"
#include "wspctl/infrastructure/protocol.hpp"

#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include <chrono>
#include <cstddef>
#include <exception>
#include <mutex>
#include <optional>
#include <stdexcept>
#include <vector>

namespace py = pybind11;

namespace wspctl {
namespace {

/** @brief Python RuntimeProcessError 类型对象 / Python RuntimeProcessError type object. */
PyObject* g_runtime_process_error_type{nullptr};
/** @brief Python InvocationInDoubtError 类型对象 / Python InvocationInDoubtError type object. */
PyObject* g_invocation_in_doubt_error_type{nullptr};

/** @brief 将 ErrorCode 映射为稳定 Python code / Map ErrorCode to a stable Python code. */
[[nodiscard]] std::string_view error_code_name(ErrorCode code);

/** @brief Python binding 向上抛出的结构化错误 / Structured error raised by the Python binding. */
class NativeFailure final : public std::runtime_error {
public:
    /**
     * @brief 构造绑定错误 / Construct a binding error.
     * @param error 原生错误 / Native error.
     * @param request_id 可选调用 ID / Optional invocation ID.
     */
    NativeFailure(Error error, std::string request_id)
        : std::runtime_error(error.message), error_(std::move(error)), request_id_(std::move(request_id)) {}

    /** @brief 取得原生错误 / Get native error. */
    [[nodiscard]] const Error& error() const noexcept { return error_; }
    /** @brief 取得调用 ID / Get invocation ID. */
    [[nodiscard]] const std::string& request_id() const noexcept { return request_id_; }

private:
    /** @brief 原生错误 / Native error. */
    Error error_;
    /** @brief 调用 ID / Invocation ID. */
    std::string request_id_;
};

/**
 * @brief 将 NativeFailure 翻译为带稳定属性的 Python 异常 / Translate NativeFailure into a Python exception with stable attributes.
 * @param pointer 当前 C++ 异常 / Current C++ exception.
 * @note pybind11 3.x 要求此 translator 可转换为普通函数指针；因此不能捕获 module-local 对象。
 *       pybind11 3.x requires this translator to be convertible to a plain function pointer, so it cannot capture module-local objects.
 */
void translate_native_failure(std::exception_ptr pointer) {
    try {
        if (pointer != nullptr) {
            std::rethrow_exception(pointer);
        }
    } catch (const NativeFailure& failure) {
        if (g_runtime_process_error_type == nullptr || g_invocation_in_doubt_error_type == nullptr) {
            PyErr_SetString(PyExc_RuntimeError, failure.what());
            return;
        }
        const PyObject* selected = failure.error().code == ErrorCode::invocation_in_doubt
            ? g_invocation_in_doubt_error_type
            : g_runtime_process_error_type;
        const py::object error_type = py::reinterpret_borrow<py::object>(const_cast<PyObject*>(selected));
        const py::object instance = error_type(py::str(failure.what()));
        instance.attr("code") = py::str(error_code_name(failure.error().code));
        instance.attr("message") = py::str(failure.error().message);
        instance.attr("request_id") = py::str(failure.request_id());
        PyErr_SetObject(error_type.ptr(), instance.ptr());
    }
}

/** @brief 将 ErrorCode 映射为稳定 Python code / Map ErrorCode to a stable Python code. */
[[nodiscard]] std::string_view error_code_name(const ErrorCode code) {
    switch (code) {
        case ErrorCode::invalid_argument: return "invalid_argument";
        case ErrorCode::malformed_frame: return "malformed_frame";
        case ErrorCode::frame_too_large: return "frame_too_large";
        case ErrorCode::unsupported_version: return "unsupported_version";
        case ErrorCode::protocol_violation: return "protocol_violation";
        case ErrorCode::authentication_failed: return "authentication_failed";
        case ErrorCode::sandbox_preflight_failed: return "sandbox_preflight_failed";
        case ErrorCode::permission_denied: return "permission_denied";
        case ErrorCode::not_found: return "not_found";
        case ErrorCode::already_exists: return "already_exists";
        case ErrorCode::busy: return "busy";
        case ErrorCode::timeout: return "timeout";
        case ErrorCode::io_failure: return "io_failure";
        case ErrorCode::journal_conflict: return "journal_conflict";
        case ErrorCode::invocation_in_doubt: return "invocation_in_doubt";
        case ErrorCode::child_failure: return "child_failure";
        case ErrorCode::internal: return "internal";
    }
    return "internal";
}

/** @brief 将 Python Iterable[bytes] 适配为单消费 native chunk 源 / Adapt a Python Iterable[bytes] into a single-consumption native chunk source. */
class PythonPayloadChunkSource final : public presentation::PayloadChunkSource {
public:
    /**
     * @brief 建立严格 bytes-only source / Construct a strict bytes-only source.
     * @param chunks Python iterable of binary chunks / Python iterable of binary chunks.
     * @return None / None.
     * @raise py::type_error 输入不是 iterable 或本身是裸 binary buffer 时抛出 /
     *     Raised when input is not iterable or is itself a raw binary buffer.
     */
    explicit PythonPayloadChunkSource(const py::handle chunks) {
        if (py::isinstance<py::str>(chunks) || py::isinstance<py::bytes>(chunks) ||
            py::isinstance<py::bytearray>(chunks) || py::isinstance<py::memoryview>(chunks)) {
            throw py::type_error("RuntimeProcess.add_file chunks must be an iterable of bytes chunks, not one raw buffer");
        }
        iterator_ = py::iter(chunks);
    }

    /**
     * @brief 取得最多一个 64 KiB bytes chunk / Obtain at most one 64 KiB bytes chunk.
     * @return 一个 bytes chunk、EOF 或结构化错误 / One bytes chunk, EOF, or a structured error.
     */
    [[nodiscard]] Result<std::optional<std::vector<std::byte>>> next_chunk() override {
        py::gil_scoped_acquire acquire;
        if (iterator_ == py::iterator::sentinel()) {
            return std::optional<std::vector<std::byte>>{};
        }
        const py::handle item = *iterator_;
        ++iterator_;
        if (!py::isinstance<py::bytes>(item)) {
            return std::unexpected(make_error(
                ErrorCode::invalid_argument,
                "RuntimeProcess.add_file chunks must contain bytes values only"));
        }
        const std::string bytes = py::cast<std::string>(item);
        if (bytes.empty() || bytes.size() > kMaxAddFileChunkBytes) {
            return std::unexpected(make_error(
                ErrorCode::invalid_argument,
                "RuntimeProcess.add_file chunk is empty or exceeds 64 KiB"));
        }
        std::vector<std::byte> chunk;
        chunk.reserve(bytes.size());
        for (const unsigned char byte : bytes) {
            chunk.push_back(static_cast<std::byte>(byte));
        }
        return std::optional<std::vector<std::byte>>{std::move(chunk)};
    }

private:
    /** @brief 仅消费一次的 Python iterator / Python iterator consumed exactly once. */
    py::iterator iterator_;
};

/**
 * @brief 非特权 Python RuntimeProcess client / Unprivileged Python RuntimeProcess client.
 *
 * 对象缓存的只是 runtime identity 与 activation；每次控制操作使用一条短 SOCK_SEQPACKET 连接，
 * 因此 Bot 退出不会持有或阻塞 broker connection。
 * The object caches only runtime identity and activation; each control operation uses a short
 * SOCK_SEQPACKET connection, so Bot shutdown never retains or blocks a broker connection.
 */
class RuntimeProcess final {
public:
    /**
     * @brief 构造惰性 client / Construct a lazy client.
     * @param socket_path broker socket 路径 / Broker socket path.
     * @param runtime_key runtime UUID / Runtime UUID.
     * @param activation_id 此 handle 唯一绑定的 activation / Activation uniquely bound to this handle.
     */
    RuntimeProcess(std::string socket_path, std::string runtime_key, std::string activation_id)
        : gateway_(std::move(socket_path)), runtime_key_(std::move(runtime_key)), activation_id_(std::move(activation_id)) {
        if (const auto endpoint = presentation::UnixGatewayClient::validate_socket_path(gateway_socket_path()); !endpoint) {
            throw NativeFailure(endpoint.error(), {});
        }
        if (const auto runtime = domain::RuntimeId::parse(runtime_key_); !runtime) {
            throw NativeFailure(make_error(ErrorCode::invalid_argument, runtime.error().message), {});
        }
        if (const auto activation = domain::ActivationId::parse(activation_id_); !activation) {
            throw NativeFailure(make_error(ErrorCode::invalid_argument, activation.error().message), {});
        }
    }

    /**
     * @brief 通过一次短连接执行命令 / Execute a command through one short connection.
     * @param argv 直接 exec argv / Direct exec argv.
     * @param stdin_data stdin / stdin.
     * @param cwd runtime cwd / Runtime cwd.
     * @param timeout_ms timeout / Timeout.
     * @param output_limit output cap / Output cap.
     * @param request_id stable invocation ID / Stable invocation ID.
     * @param request_hash caller semantic hash / Caller semantic hash.
     * @return Python result dictionary / Python result dictionary.
     */
    [[nodiscard]] py::dict execute(
        const py::sequence& argv,
        const std::string& stdin_data,
        const std::string& cwd,
        const std::int64_t timeout_ms,
        const std::size_t output_limit,
        const std::string& request_id,
        const std::string& request_hash) {
        std::lock_guard lock(mutex_);
        if (closed_) {
            throw NativeFailure(make_error(ErrorCode::permission_denied, "RuntimeProcess is closed"), request_id);
        }
        presentation::ClientExecuteRequest request;
        request.runtime_key = runtime_key_;
        request.activation_id = activation_id_;
        request.request_id = request_id;
        request.request_hash = request_hash;
        request.stdin_data = stdin_data;
        request.cwd = cwd;
        request.timeout = std::chrono::milliseconds(timeout_ms);
        request.output_limit = output_limit;
        request.argv.reserve(py::len(argv));
        for (const py::handle item : argv) {
            request.argv.push_back(py::cast<std::string>(item));
        }
        presentation::ClientExecutionResult result;
        {
            py::gil_scoped_release release;
            const auto executed = gateway_.execute(request);
            if (!executed) {
                throw NativeFailure(executed.error(), request_id);
            }
            result = *executed;
        }
        py::dict dictionary;
        dictionary["stdout"] = result.stdout_data;
        dictionary["stderr"] = result.stderr_data;
        dictionary["exit_code"] = result.exit_code.has_value() ? py::cast(*result.exit_code) : py::none();
        dictionary["timed_out"] = result.timed_out;
        dictionary["truncated"] = result.truncated;
        dictionary["replayed"] = result.replayed;
        dictionary["request_id"] = result.request_id;
        return dictionary;
    }

    /**
     * @brief 向受限 workspace 路径流式写入一个文件 / Stream one file into a constrained workspace path.
     * @param opaque_id 可信上层生成的 opaque directory capability / Opaque directory capability generated by a trusted upper layer.
     * @param chunks 单消费 ``Iterable[bytes]`` / Single-consumption ``Iterable[bytes]``.
     * @param byte_size 声明完整文件字节数 / Declared complete file byte count.
     * @param sha256 声明完整文件 SHA-256 / Declared complete file SHA-256.
     * @param request_id 稳定 journal 调用 ID / Stable journal invocation ID.
     * @param request_hash 调用方语义 SHA-256 / Caller semantic SHA-256.
     * @return Python 文件收据 dictionary / Python file-receipt dictionary.
     * @note 该入口只转运 bytes；它不会解释 MIME、扩展名或 shebang，也不会接受 host path。
     *     This entry transports bytes only; it does not interpret MIME, extensions, or shebangs,
     *     and it accepts no host path.
     */
    [[nodiscard]] py::dict add_file(
        const std::string& opaque_id,
        const py::handle chunks,
        const std::size_t byte_size,
        const std::string& sha256,
        const std::string& request_id,
        const std::string& request_hash) {
        std::lock_guard lock(mutex_);
        if (closed_) {
            throw NativeFailure(make_error(ErrorCode::permission_denied, "RuntimeProcess is closed"), request_id);
        }
        PythonPayloadChunkSource source(chunks);
        presentation::ClientAddFileRequest request{
            .runtime_key = runtime_key_,
            .activation_id = activation_id_,
            .request_id = request_id,
            .request_hash = request_hash,
            .opaque_id = opaque_id,
            .byte_size = byte_size,
            .sha256 = sha256,
        };
        presentation::ClientAddFileResult result;
        {
            py::gil_scoped_release release;
            const auto added = gateway_.add_file(request, source);
            if (!added) {
                throw NativeFailure(added.error(), request_id);
            }
            result = *added;
        }
        py::dict dictionary;
        dictionary["request_id"] = result.request_id;
        dictionary["replayed"] = result.replayed;
        dictionary["path"] = result.path;
        dictionary["byte_size"] = result.byte_size;
        dictionary["sha256"] = result.sha256;
        return dictionary;
    }

    /**
     * @brief 只读恢复已完成 add_file 的 durable receipt / Read-only replay of a completed add_file durable receipt.
     * @param opaque_id 可信上层保存的 opaque directory capability / Opaque directory capability persisted by the trusted upper layer.
     * @param byte_size 已保存的完整文件字节数 / Persisted complete file byte count.
     * @param sha256 已保存的完整文件 SHA-256 / Persisted complete file SHA-256.
     * @param request_id 原始稳定 journal 调用 ID / Original stable journal invocation ID.
     * @param request_hash 原始调用方语义 SHA-256 / Original caller semantic SHA-256.
     * @return ``replayed=true`` 的 Python 文件收据 dictionary / Python file-receipt dictionary with ``replayed=true``.
     * @note 该入口刻意不使用此 handle 的 activation；它不会启动/替换 RuntimeProcess，也不会
     *       读取 Python bytes 或创建 pending journal。/ This entry deliberately does not use this
     *       handle's activation; it does not start/replace a RuntimeProcess, read Python bytes,
     *       or create a pending journal.
     */
    [[nodiscard]] py::dict replay_file(
        const std::string& opaque_id,
        const std::size_t byte_size,
        const std::string& sha256,
        const std::string& request_id,
        const std::string& request_hash) {
        std::lock_guard lock(mutex_);
        if (closed_) {
            throw NativeFailure(make_error(ErrorCode::permission_denied, "RuntimeProcess is closed"), request_id);
        }
        presentation::ClientReplayFileRequest request{
            .runtime_key = runtime_key_,
            .request_id = request_id,
            .request_hash = request_hash,
            .opaque_id = opaque_id,
            .byte_size = byte_size,
            .sha256 = sha256,
        };
        presentation::ClientAddFileResult result;
        {
            py::gil_scoped_release release;
            const auto replayed = gateway_.replay_file(request);
            if (!replayed) {
                throw NativeFailure(replayed.error(), request_id);
            }
            result = *replayed;
        }
        if (!result.replayed) {
            throw NativeFailure(make_error(ErrorCode::protocol_violation, "broker returned a non-replayed receipt for replay_file"), request_id);
        }
        py::dict dictionary;
        dictionary["request_id"] = result.request_id;
        dictionary["replayed"] = result.replayed;
        dictionary["path"] = result.path;
        dictionary["byte_size"] = result.byte_size;
        dictionary["sha256"] = result.sha256;
        return dictionary;
    }

    /** @brief 关闭逻辑 handle / Close the logical handle. */
    void close() noexcept {
        std::lock_guard lock(mutex_);
        closed_ = true;
    }

private:
    /** @brief 取得 gateway endpoint 供构造时验证 / Get the gateway endpoint for construction-time validation. */
    [[nodiscard]] const std::string& gateway_socket_path() const noexcept {
        return gateway_.socket_path();
    }

    /** @brief presentation Unix gateway client / Presentation Unix gateway client. */
    presentation::UnixGatewayClient gateway_;
    /** @brief 持久 runtime UUID / Persistent runtime UUID. */
    std::string runtime_key_;
    /** @brief handle 生命周期内稳定 activation / Stable activation for handle lifetime. */
    std::string activation_id_;
    /** @brief 保护 close、execute 与 add_file / Protect close, execute, and add_file. */
    std::mutex mutex_;
    /** @brief 是否已关闭 / Whether the handle was closed. */
    bool closed_{false};
};

}  // namespace
}  // namespace wspctl

PYBIND11_MODULE(_native, module) {
    module.doc() = "wspctl non-privileged SOCK_SEQPACKET client";
    py::object runtime_error_type = py::reinterpret_steal<py::object>(
        PyErr_NewException("wspctl._native.RuntimeProcessError", PyExc_RuntimeError, nullptr));
    py::object in_doubt_type = py::reinterpret_steal<py::object>(
        PyErr_NewException("wspctl._native.InvocationInDoubtError", runtime_error_type.ptr(), nullptr));
    module.attr("RuntimeProcessError") = runtime_error_type;
    module.attr("InvocationInDoubtError") = in_doubt_type;
    module.attr("NativeError") = runtime_error_type;
    wspctl::g_runtime_process_error_type = runtime_error_type.ptr();
    wspctl::g_invocation_in_doubt_error_type = in_doubt_type.ptr();
    py::register_exception_translator(&wspctl::translate_native_failure);
    py::class_<wspctl::RuntimeProcess>(module, "RuntimeProcess")
        .def(
            py::init<std::string, std::string, std::string>(),
            py::arg("socket_path"),
            py::arg("runtime_key"),
            py::arg("activation_id"))
        .def(
            "execute",
            &wspctl::RuntimeProcess::execute,
            py::arg("argv"),
            py::arg("stdin") = "",
            py::arg("cwd") = "/workspace",
            py::arg("timeout_ms") = 30'000,
            py::arg("output_limit") = 65'536,
            py::arg("request_id") = "",
            py::arg("request_hash") = "")
        .def(
            "add_file",
            &wspctl::RuntimeProcess::add_file,
            py::arg("opaque_id"),
            py::arg("chunks"),
            py::arg("byte_size"),
            py::arg("sha256"),
            py::arg("request_id") = "",
            py::arg("request_hash") = "")
        .def(
            "replay_file",
            &wspctl::RuntimeProcess::replay_file,
            py::arg("opaque_id"),
            py::arg("byte_size"),
            py::arg("sha256"),
            py::arg("request_id") = "",
            py::arg("request_hash") = "")
        .def("close", &wspctl::RuntimeProcess::close);
}
