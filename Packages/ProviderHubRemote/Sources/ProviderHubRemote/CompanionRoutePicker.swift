import SwiftUI
#if os(iOS)
import UIKit
#endif

struct CompanionPickerChoice: ChatPickerChoice {
    let row: RemoteValue
    var id: String { row["id"].text }
    var label: String { row["label"].text }
    var accountLabel: String { row["accountLabel"].text }
    var context: Int? { row["context"].optionalInt }
    var efforts: [String] { row["efforts"].rows.map(\.text).filter { !$0.isEmpty } }
    var connectionID: String {
        let supplied = row["connectionPresentation"]["runtimeProvider"].text
        return supplied.isEmpty ? String(row["route"].text.split(separator: "/").first ?? "") : supplied
    }
    var connectionLabel: String {
        let supplied = row["connectionPresentation"]["displayProvider"].text
        return supplied.isEmpty ? connectionID.capitalized : supplied
    }
    var presentation: CompanionPresentation {
        row["presentation"] == .null ? CompanionBranding.presentation(route: row["route"].text, label: label) : CompanionPresentation(row["presentation"], fallback: label)
    }
    var connectionPresentation: CompanionPresentation {
        row["connectionPresentation"] == .null ? CompanionBranding.presentation(route: connectionID + "/", label: connectionLabel) : CompanionPresentation(row["connectionPresentation"], fallback: connectionLabel)
    }
    var accent: Color { presentation.accent }
    var connectionAccent: Color { connectionPresentation.accent }
}

struct CompanionRoutePicker: View {
    @ObservedObject var model: CompanionModel
    var initialChoiceID: String? = nil
    var initialEffort: String? = nil
    var toolsOnly = false
    var choose: (RemoteValue, String) -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var owner: String?
    var body: some View {
        NavigationStack {
            ChatSharedModelPicker(choices: model.models.filter { !toolsOnly || $0["supportsTools"].flag }.map(CompanionPickerChoice.init),
                initialChoiceID: initialChoiceID ?? model.configuration["choice"].text,
                initialEffort: initialEffort ?? model.configuration["effort"].text,
                disabled: !model.isConnected,
                refresh: { Task { @MainActor in await model.refreshRemoteOptions() } },
                choose: { id, effort in
                    guard owner == model.selectedChatID, let row = model.models.first(where: { $0["id"].text == id }) else { dismiss(); return }
                    choose(row, effort); dismiss()
                }) { row, connection, size in
                    CompanionProviderMark(presentation: connection ? row.connectionPresentation : row.presentation, size: size)
                }
                .navigationTitle("Models")
                #if os(iOS)
                .navigationBarTitleDisplayMode(.inline)
                #endif
                .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Done") { dismiss() } } }
        }
        .presentationDetents([.height(500), .large])
        .onAppear { owner = model.selectedChatID }
    }
}
