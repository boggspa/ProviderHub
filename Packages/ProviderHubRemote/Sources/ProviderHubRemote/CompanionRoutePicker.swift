import SwiftUI

/// The desktop's searchable model/account rows, presented as a touch sheet.
struct CompanionRoutePicker: View {
    @ObservedObject var model: CompanionModel
    var choose: (RemoteValue) -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var search = ""
    private var rows: [RemoteValue] {
        model.models.filter { row in
            search.isEmpty || [row["label"].text, row["provider"].text, row["accountLabel"].text, row["route"].text]
                .joined(separator: " ").localizedCaseInsensitiveContains(search)
        }
    }
    var body: some View {
        NavigationStack {
            List(rows, id: \.self) { row in
                Button {
                    choose(row); dismiss()
                } label: {
                    HStack(spacing: 10) {
                        CompanionProviderMark(presentation: CompanionBranding.presentation(route: row["route"].text, label: row["label"].text, models: model.models), size: 24)
                        VStack(alignment: .leading, spacing: 3) {
                            Text(row["label"].text).font(.system(size: 13, weight: .medium)).foregroundStyle(CompanionTheme.ink)
                            Text([row["provider"].text, row["accountLabel"].text].filter { !$0.isEmpty }.joined(separator: " · "))
                                .font(.system(size: 11)).foregroundStyle(CompanionTheme.secondary)
                        }
                        Spacer()
                        if row["id"] == model.configuration["choice"] { Image(systemName: "checkmark").foregroundStyle(CompanionTheme.brand) }
                    }.padding(.vertical, 4)
                }.buttonStyle(.plain)
            }
            .searchable(text: $search, prompt: "Search models and accounts")
            .navigationTitle("Models")
            .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Done") { dismiss() } } }
        }
    }
}
