import AppKit
import SwiftUI

struct ChatGitRepository: Decodable {
    var host: String; var owner: String; var name: String; var url: String
    var pullsURL: URL? { URL(string: url + "/pulls") }
}
struct ChatGitCommitCursor: Decodable {
    var head: String; var upstream: String?; var root: String; var repository: String?; var offset: Int
    var wire: [String: Any] {
        ["head": head, "upstream": upstream as Any? ?? NSNull(), "root": root,
         "repository": repository as Any? ?? NSNull(), "offset": offset]
    }
}
struct ChatGitLogCommit: Decodable, Identifiable {
    var hash: String; var subject: String; var author: String; var time: String
    var id: String { hash }
}
struct ChatGitOverviewSnapshot: Decodable {
    var root: String; var head: String?; var branch: String; var upstream: String?
    var repository: ChatGitRepository?; var commits: [ChatGitLogCommit]
    var nextCursor: ChatGitCommitCursor?; var truncated: Bool
}
struct ChatGitPullRequest: Decodable, Identifiable {
    var number: Int; var title: String; var url: String; var branch: String; var author: String; var draft: Bool
    var id: Int { number }
    var link: URL? {
        guard let value = URL(string: url), value.scheme == "https" else { return nil }
        return value
    }
}
struct ChatGitPullCursor: Decodable {
    var repository: String; var after: String
    var wire: [String: Any] { ["repository": repository, "after": after] }
}
struct ChatGitPullRequestsSnapshot: Decodable {
    var items: [ChatGitPullRequest]; var repository: ChatGitRepository?; var nextCursor: ChatGitPullCursor?; var notice: String
}
struct ChatGitOverviewState {
    var snapshot: ChatGitOverviewSnapshot?
    var pullRequests: ChatGitPullRequestsSnapshot?
    var request: String?; var pullRequest: String?
    var appending = false; var appendingPulls = false
    var notice = ""; var pullsNotice = ""
}

@MainActor extension ChatModel {
    func refreshGitOverview(more: Bool = false) {
        guard connected, let chat = stateChatID, gitOverview.request == nil else { return }
        var state = gitOverview
        var command: [String: Any] = ["command": "git_overview", "id": chat, "request": UUID().uuidString]
        if more {
            guard let cursor = state.snapshot?.nextCursor else { return }
            command["cursor"] = cursor.wire
        }
        state.request = command["request"] as? String; state.appending = more; state.notice = ""
        gitOverview = state
        if !inspectorCommand(command) { gitOverview.request = nil; gitOverview.notice = "Chat is disconnected." }
    }
    func refreshPullRequests(more: Bool = false) {
        guard connected, let chat = stateChatID, gitOverview.pullRequest == nil else { return }
        var state = gitOverview
        var command: [String: Any] = ["command": "git_pull_requests", "id": chat, "request": UUID().uuidString]
        if more {
            guard let cursor = state.pullRequests?.nextCursor, (state.pullRequests?.items.count ?? 0) < 200 else { return }
            command["cursor"] = cursor.wire
        }
        state.pullRequest = command["request"] as? String; state.appendingPulls = more; state.pullsNotice = ""
        gitOverview = state
        if !inspectorCommand(command) { gitOverview.pullRequest = nil; gitOverview.pullsNotice = "Chat is disconnected." }
    }
    func consumeGitOverview(_ event: [String: Any]) {
        guard event["chat"] as? String == stateChatID, event["workspace"] as? String == selected?.workspace else { return }
        func decode<T: Decodable>(_ key: String, _ type: T.Type) -> T? {
            guard let value = event[key], JSONSerialization.isValidJSONObject(value),
                  let data = try? JSONSerialization.data(withJSONObject: value) else { return nil }
            return try? JSONDecoder().decode(type, from: data)
        }
        var state = gitOverview
        if event["event"] as? String == "git_overview" {
            guard let request = state.request, event["request"] as? String == request else { return }
            state.request = nil; state.notice = event["notice"] as? String ?? ""
            if var snapshot = decode("overview", ChatGitOverviewSnapshot.self) {
                if state.appending, let previous = state.snapshot {
                    guard previous.head == snapshot.head, previous.upstream == snapshot.upstream,
                          previous.root == snapshot.root, previous.repository?.url == snapshot.repository?.url else {
                        state.notice = "Git history changed. Refresh before loading more commits."; gitOverview = state; return
                    }
                    let known = Set(previous.commits.map(\.id))
                    snapshot.commits = previous.commits + snapshot.commits.filter { !known.contains($0.id) }
                }
                if let pulls = state.pullRequests, pulls.repository?.url != snapshot.repository?.url {
                    state.pullRequests = nil
                    state.pullsNotice = "Repository changed. Refresh pull requests."
                }
                state.snapshot = snapshot
            }
        } else {
            guard let request = state.pullRequest, event["request"] as? String == request else { return }
            state.pullRequest = nil; state.pullsNotice = event["notice"] as? String ?? ""
            if var snapshot = decode("pullRequests", ChatGitPullRequestsSnapshot.self) {
                if let repository = state.snapshot?.repository, repository.url != snapshot.repository?.url {
                    state.pullsNotice = "Repository changed. Refresh pull requests."; gitOverview = state; return
                }
                if state.appendingPulls, let previous = state.pullRequests {
                    guard previous.repository?.url == snapshot.repository?.url else {
                        state.pullsNotice = "Repository changed. Refresh pull requests."; gitOverview = state; return
                    }
                    let known = Set(previous.items.map(\.id))
                    snapshot.items = Array((previous.items + snapshot.items.filter { !known.contains($0.id) }).prefix(200))
                }
                state.pullRequests = snapshot
            }
        }
        gitOverview = state
    }
}

/// A bounded, independently scrolling viewport below this turn's file changes.
/// Git and GitHub run in worker threads; this view only renders small pages.
struct ChatGitOverviewPane: View {
    @ObservedObject var model: ChatModel
    private var state: ChatGitOverviewState { model.gitOverview }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 6) {
                Label("Repository", systemImage: "arrow.triangle.branch").font(.system(size: 11.5, weight: .semibold))
                Spacer(minLength: 4)
                if state.request != nil || state.pullRequest != nil { ProgressView().controlSize(.mini) }
                Button { model.refreshGitOverview(); model.refreshPullRequests(); model.refreshGitStatus() } label: {
                    Image(systemName: "arrow.clockwise")
                }.buttonStyle(.plain).disabled(!model.connected || state.request != nil || state.pullRequest != nil)
                    .help("Refresh repository information").accessibilityLabel("Refresh repository information")
            }.padding(.horizontal, 14).padding(.vertical, 10)
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 10) {
                    if let snapshot = state.snapshot {
                        HStack(spacing: 5) {
                            Text(snapshot.branch).font(.system(size: 11, weight: .medium, design: .monospaced))
                                .lineLimit(1).truncationMode(.middle)
                            if let upstream = snapshot.upstream {
                                Text("· " + upstream).font(.system(size: 10.5)).foregroundStyle(.secondary)
                                    .lineLimit(1).truncationMode(.middle)
                            }
                        }.help(snapshot.root)
                        Text(snapshot.upstream == nil ? "Local commits" : "Unpushed commits")
                            .font(.system(size: 11, weight: .medium)).foregroundStyle(.secondary)
                        if snapshot.upstream == nil, snapshot.head != nil {
                            note("No upstream is set; these are local commits.")
                        }
                        if snapshot.commits.isEmpty { note(snapshot.head == nil ? "No commits yet." : "No unpushed commits.") }
                        ForEach(snapshot.commits) { commit in commitRow(commit, repository: snapshot.repository) }
                        if snapshot.nextCursor != nil {
                            Button("Load more commits") { model.refreshGitOverview(more: true) }
                                .buttonStyle(.link).font(.system(size: 11)).disabled(state.request != nil)
                        }
                        if snapshot.truncated { note("Showing the first 200 commits.") }
                    } else { note(state.request != nil ? "Reading local Git information…" : "Choose a Git workspace.") }
                    if !state.notice.isEmpty { note(state.notice) }
                    Divider().padding(.vertical, 2)
                    HStack {
                        Text("Open pull requests").font(.system(size: 11, weight: .medium)).foregroundStyle(.secondary)
                        Spacer(minLength: 4)
                        if let link = (state.pullRequests?.repository ?? state.snapshot?.repository)?.pullsURL {
                            Link("GitHub ↗", destination: link).font(.system(size: 10.5))
                        }
                    }
                    if let pulls = state.pullRequests {
                        if pulls.items.isEmpty && pulls.notice.isEmpty { note("No open pull requests.") }
                        ForEach(pulls.items) { pull in pullRow(pull) }
                        if !pulls.notice.isEmpty { note(pulls.notice) }
                        if pulls.nextCursor != nil, pulls.items.count < 200 {
                            Button("Load more pull requests") { model.refreshPullRequests(more: true) }
                                .buttonStyle(.link).font(.system(size: 11)).disabled(state.pullRequest != nil)
                        } else if pulls.nextCursor != nil { note("Showing the first 200 pull requests. Open GitHub to see all.") }
                    } else if state.pullRequest != nil { note("Looking up pull requests…") }
                    if !state.pullsNotice.isEmpty { note(state.pullsNotice) }
                }.padding(.horizontal, 14).padding(.bottom, 14)
            }
        }
        .task(id: "\(model.selectedID ?? "")|\(model.selected?.workspace ?? "")|\(model.connected)") {
            model.refreshGitOverview(); model.refreshPullRequests()
        }
        .onChange(of: model.busy) { _, busy in if !busy { model.refreshGitOverview() } }
        .accessibilityLabel("Repository information")
    }
    private func note(_ value: String) -> some View {
        Text(value).font(.system(size: 10.5)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
    }
    private func commitRow(_ commit: ChatGitLogCommit, repository: ChatGitRepository?) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            if let repository, let url = URL(string: repository.url + "/commit/" + commit.hash) {
                Link(destination: url) { Text(commit.subject).foregroundStyle(.primary).lineLimit(2) }
            } else { Text(commit.subject).lineLimit(2) }
            Text(String(commit.hash.prefix(7)) + " · " + commit.author).font(.system(size: 10)).foregroundStyle(.secondary).lineLimit(1)
        }.font(.system(size: 11.5)).frame(maxWidth: .infinity, alignment: .leading)
            .help(commit.subject).textSelection(.enabled)
    }
    @ViewBuilder private func pullRow(_ pull: ChatGitPullRequest) -> some View {
        if let url = pull.link {
            Link(destination: url) {
                VStack(alignment: .leading, spacing: 3) {
                    HStack(alignment: .top, spacing: 5) {
                        Image(systemName: "arrow.triangle.pull").font(.system(size: 10)).foregroundStyle(pull.draft ? Color.secondary : .green)
                        Text(pull.title).foregroundStyle(.primary).lineLimit(2)
                    }
                    Text("#\(pull.number) · " + pull.author + (pull.draft ? " · Draft" : ""))
                        .font(.system(size: 10)).foregroundStyle(.secondary).lineLimit(1)
                }.font(.system(size: 11.5)).frame(maxWidth: .infinity, alignment: .leading)
            }.help(pull.branch).accessibilityLabel("Pull request \(pull.number): \(pull.title)")
        }
    }
}
