import AppKit
import SwiftUI

struct ChatBranchChip: View {
    @ObservedObject var model: ChatModel
    @State private var showing = false
    var body: some View {
        Button { showing = true; model.refreshBranches() } label: {
            HStack(spacing: 4) {
                Image(systemName: "arrow.triangle.branch").font(.system(size: 11))
                Text(model.branches?.current ?? model.gitStatus?.branch ?? "Branch")
                    .font(HubTheme.Typography.detail).lineLimit(1).truncationMode(.middle)
                Image(systemName: "chevron.down").font(.system(size: 8))
            }.foregroundStyle(.secondary).frame(maxWidth: 150, alignment: .leading)
        }.buttonStyle(.plain).disabled(!model.connected || model.selectedID == nil)
            .accessibilityLabel("Branch and worktree").help("Choose or create a branch or worktree")
            .popover(isPresented: $showing, arrowEdge: .bottom) { ChatBranchesPicker(model: model) { showing = false } }
            .task(id: model.selected?.workspace) { model.refreshBranches() }
    }
}

private struct ChatBranchesPicker: View {
    @ObservedObject var model: ChatModel
    var dismiss: () -> Void
    @State private var creating: String?
    @State private var branchName = "codex/"
    @State private var worktreePath = ""
    @State private var pendingAction: String?
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text("Branches & Worktrees").font(.system(size: 12, weight: .semibold))
                Spacer()
                if model.branchesLoading || model.branchBusy { ProgressView().controlSize(.mini) }
                Button { model.refreshBranches() } label: { Image(systemName: "arrow.clockwise") }
                    .buttonStyle(.plain).disabled(model.branchesLoading || model.branchBusy).accessibilityLabel("Refresh branches")
            }
            if let snapshot = model.branches {
                ScrollView {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Branches").font(.system(size: 10.5, weight: .medium)).foregroundStyle(.secondary).padding(.bottom, 4)
                        if snapshot.branches.isEmpty { Text("No commits yet").font(.system(size: 11.5)).foregroundStyle(.secondary).padding(.vertical, 4) }
                        ForEach(snapshot.branches) { branch in
                            Button {
                                pendingAction = "switch"; model.branchAction("switch", branch: branch.name, branchBytes: branch.nameBytes)
                            } label: {
                                HStack {
                                    Text(branch.name).lineLimit(1).truncationMode(.middle)
                                    Spacer(minLength: 4)
                                    if branch.current { Image(systemName: "checkmark") }
                                    else if branch.worktree != nil { Image(systemName: "folder") }
                                }.contentShape(Rectangle()).padding(.vertical, 5)
                            }.buttonStyle(.plain).disabled(!model.canChangeBranch || branch.current || (branch.worktree != nil && branch.worktree != snapshot.root))
                                .help(branch.worktree.map { "Checked out in " + $0 } ?? branch.name)
                        }
                        Divider().padding(.vertical, 7)
                        Text("Worktrees").font(.system(size: 10.5, weight: .medium)).foregroundStyle(.secondary).padding(.bottom, 4)
                        ForEach(snapshot.worktrees) { tree in
                            Button {
                                pendingAction = "select_worktree"; model.branchAction("select_worktree", path: tree.path)
                            } label: {
                                HStack(spacing: 8) {
                                    VStack(alignment: .leading, spacing: 2) {
                                        Text(tree.branch ?? "Detached HEAD").lineLimit(1)
                                        Text((tree.path as NSString).abbreviatingWithTildeInPath).font(.system(size: 10)).foregroundStyle(.secondary).lineLimit(1).truncationMode(.middle)
                                    }
                                    Spacer(minLength: 0)
                                    if tree.current { Image(systemName: "checkmark") }
                                    else if tree.prunable == true { Image(systemName: "exclamationmark.triangle") }
                                }.contentShape(Rectangle()).padding(.vertical, 5)
                            }.buttonStyle(.plain).disabled(!model.canChangeBranch || (tree.current && tree.path == model.selected?.workspace) || tree.prunable == true || tree.selectable == false)
                                .help(tree.selectable == false ? "This path cannot be represented by macOS. Use Git to relocate the worktree." : tree.path)
                        }
                    }.font(.system(size: 12))
                }.frame(maxHeight: 250)
                Divider()
                if let creating {
                    TextField("New branch name", text: $branchName).textFieldStyle(.roundedBorder)
                        .onSubmit { submit() }.accessibilityLabel("New branch name")
                    if creating == "worktree" {
                        Button {
                            let panel = NSSavePanel(); panel.canCreateDirectories = true; panel.prompt = "Choose location"
                            panel.title = "New worktree location"
                            panel.nameFieldStringValue = branchName.split(separator: "/").last.map(String.init) ?? "worktree"
                            panel.directoryURL = URL(fileURLWithPath: snapshot.root).deletingLastPathComponent()
                            if panel.runModal() == .OK, let url = panel.url { worktreePath = url.path }
                        } label: {
                            Label(worktreePath.isEmpty ? "Choose location…" : (worktreePath as NSString).abbreviatingWithTildeInPath, systemImage: "folder")
                                .lineLimit(1).truncationMode(.middle)
                        }.buttonStyle(.plain).font(.system(size: 11.5))
                    }
                    HStack {
                        Button("Cancel") { self.creating = nil }.controlSize(.small)
                        Spacer()
                        Button(creating == "worktree" ? "Create worktree" : "Create branch") { submit() }
                            .controlSize(.small).buttonStyle(.borderedProminent)
                            .disabled(!model.canChangeBranch || branchName.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || (creating == "worktree" && worktreePath.isEmpty))
                    }
                } else {
                    HStack {
                        Button("New branch…") { creating = "create" }.buttonStyle(.plain)
                        Spacer()
                        Button("New worktree…") { creating = "worktree" }.buttonStyle(.plain)
                    }.font(.system(size: 11.5)).disabled(!model.canChangeBranch)
                }
                if model.busy || model.sideChat?.busy == true { Text("Available when active turns finish.").font(.system(size: 10.5)).foregroundStyle(.secondary) }
            } else if model.branchNotice.isEmpty { Text("Reading repository…").font(.system(size: 12)).foregroundStyle(.secondary) }
            if !model.branchNotice.isEmpty { Text(model.branchNotice).font(.system(size: 11)).foregroundStyle(.secondary).textSelection(.enabled) }
        }.padding(16).frame(width: 350)
            .onChange(of: model.branchBusy) { _, busy in
                if !busy, model.branchNotice.isEmpty, let action = pendingAction {
                    if action == "worktree" { creating = nil } else { dismiss() }
                }
                if !busy { pendingAction = nil }
            }
    }
    private func submit() {
        guard let creating, model.canChangeBranch else { return }
        pendingAction = creating
        model.branchAction(creating, branch: branchName, path: creating == "worktree" ? worktreePath : nil)
    }
}
