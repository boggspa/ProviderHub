import SwiftUI

extension ChatRoute: ChatPickerChoice {
    var speedOptions: [ChatPickerSpeed] { (speeds ?? []).map { ChatPickerSpeed(id: $0.id, label: $0.label, detail: $0.detail) } }
}

/// The speed handed over just before commit, read synchronously by it.
private final class ChatPendingSpeed { var value: String? }

/// Desktop adapter to the canonical Mac/iOS provider-rail model picker.
struct ChatModelPicker: View {
    /// One height for every Mac picker (main chat, Team, Side Chat): tall
    /// enough for about eight model rows and a full Reasoning + Speed column.
    static let height: CGFloat = 500
    @ObservedObject var model: ChatModel
    var dismiss: () -> Void
    var initialChoiceID: String? = nil
    var initialEffort: String? = nil
    var onSelect: ((String, String) -> Void)? = nil
    /// A host-owned speed (a Team member's). Supplying the handler shows the
    /// Speed section; it is called with the chosen speed just before onSelect.
    var initialSpeed: String? = nil
    var onSelectSpeed: ((String) -> Void)? = nil
    @State private var ownerID: String?
    @State private var pending = ChatPendingSpeed()
    var body: some View {
        ChatSharedModelPicker(choices: model.models, initialChoiceID: initialChoiceID ?? model.selectedRoute?.id,
            initialEffort: initialEffort ?? model.selected?.effort ?? "",
            disabled: onSelect == nil && (model.busy || model.branchBusy),
            // The main chat and Team members own a speed; Side Chats keep Standard.
            initialSpeed: onSelect == nil ? model.selected?.speed ?? "" : initialSpeed ?? "",
            chooseSpeed: onSelect == nil || onSelectSpeed != nil ? { [pending] in pending.value = $0 } : nil,
            refresh: model.refresh, choose: commit) { row, connection, size in
                if let presentation = connection ? row.connectionPresentation ?? row.presentation : row.presentation {
                    ProviderMark(presentation: presentation, size: size)
                } else { Image(systemName: "sparkle").font(.system(size: size - 2)).foregroundStyle(.secondary) }
            }
            .frame(width: 520, height: ChatModelPicker.height)
            .onAppear { ownerID = model.selectedID; model.refresh() }
            .onExitCommand(perform: dismiss)
    }
    private func commit(_ id: String, _ effort: String) {
        guard ownerID == model.selectedID, let choice = model.models.first(where: { $0.id == id }) else { dismiss(); return }
        if let onSelect {
            if let onSelectSpeed { onSelectSpeed(pending.value ?? "") }
            onSelect(choice.id, effort)
        }
        else if choice.id == model.selectedRoute?.id && choice.scope == model.selected?.scope { model.setEffort(effort, speed: pending.value) }
        else { model.setSelection(choice.id, effort: effort, speed: pending.value) }
        dismiss()
    }
}
