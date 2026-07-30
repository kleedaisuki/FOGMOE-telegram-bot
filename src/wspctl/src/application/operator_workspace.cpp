#include "wspctl/application/operator_workspace.hpp"

#include <utility>

namespace wspctl::application {

OperatorWorkspaceQueryError
make_operator_workspace_query_error(const OperatorWorkspaceQueryErrorCode code,
                                    std::string message) {
    return OperatorWorkspaceQueryError{.code = code, .message = std::move(message)};
}

OperatorWorkspaceQueryResult<domain::OperatorWorkspaceStatus>
OperatorWorkspaceQueryService::status(const domain::RuntimeId& runtime,
                                      const OperatorWorkspaceReadPort& port) const {
    auto status = port.status(runtime);
    if (!status) {
        return std::unexpected(status.error());
    }
    if (status->runtime() != runtime) {
        return std::unexpected(make_operator_workspace_query_error(
            OperatorWorkspaceQueryErrorCode::inconsistent,
            "operator workspace status runtime does not match the requested runtime"));
    }
    return status;
}

OperatorWorkspaceQueryResult<domain::WorkspaceListing>
OperatorWorkspaceQueryService::list(const domain::RuntimeId& runtime,
                                    const domain::OperatorWorkspacePath& path,
                                    const OperatorWorkspaceReadPort& port) const {
    auto listing = port.list(runtime, path);
    if (!listing) {
        return std::unexpected(listing.error());
    }
    if (listing->path != path || listing->entries.size() > domain::kOperatorWorkspaceListingLimit) {
        return std::unexpected(make_operator_workspace_query_error(
            OperatorWorkspaceQueryErrorCode::inconsistent,
            "operator workspace listing violates its path or entry bound invariant"));
    }
    return listing;
}

} // namespace wspctl::application
