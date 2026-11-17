import SwiftUI

extension ChatRoute: ChatPickerChoice {
    var speedOptions: [ChatPickerSpeed] { (speeds ?? []).map { ChatPickerSpeed(id: $0.id, label: $0.label, detail: $0.detail) } }
}

/// The speed handed over just before commit, read synchronously by it.
private final class ChatPendingSpeed { var value: String? }

/// Desktop adapter to the canonical Mac/iOS provider-rail model picker.
struct ChatModelPicker: View {
    @ObservedObject var model: ChatModel
    var dismiss: () -> Void
    var initialChoiceID: String? = nil
    var initialEffort: String? = nil
    var onSelect: ((String, String) -> Void)? = nil
    @State private var ownerID: String?
    @State private var pending = ChatPendingSpeed()
    var body: some View {
        ChatSharedModelPicker(choices: model.models, initialChoiceID: initialChoiceID ?? model.selectedRoute?.id,
            initialEffort: initialEffort ?? model.selected?.effort ?? "",
            disabled: onSelect == nil && (model.busy || model.branchBusy),
            // Speed belongs to the main chat; Side Chat and Team pickers keep Standard.
            initialSpeed: onSelect == nil ? model.selected?.speed ?? "" : "",
            chooseSpeed: onSelect == nil ? { [pending] in pending.value = $0 } : nil,
            refresh: model.refresh, choose: commit) { row, connection, size in
                if let presentation = connection ? row.connectionPresentation ?? row.presentation : row.presentation {
                    ProviderMark(presentation: presentation, size: size)
                } else { Image(systemName: "sparkle").font(.system(size: size - 2)).foregroundStyle(.secondary) }
            }
            .frame(width: 520, height: 388)
            .onAppear { ownerID = model.selectedID; model.refresh() }
            .onExitCommand(perform: dismiss)
    }
    private func commit(_ id: String, _ effort: String) {
        guard ownerID == model.selectedID, let choice = model.models.first(where: { $0.id == id }) else { dismiss(); return }
        if let onSelect { onSelect(choice.id, effort) }
        else if choice.id == model.selectedRoute?.id && choice.scope == model.selected?.scope { model.setEffort(effort, speed: pending.value) }
        else { model.setSelection(choice.id, effort: effort, speed: pending.value) }
        dismiss()
    }
}
