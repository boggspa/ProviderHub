import SwiftUI

/// Native adaptation of TaskWraith's CombinedModelPicker: provider icon rail,
/// compact model list, and reasoning sidecar. Only the launcher's enabled
/// routes arrive here. Identity/accents come from the shared branding contract.
struct ChatModelPicker: View {
    @ObservedObject var model: ChatModel
    var dismiss: () -> Void
    @State private var provider = ""
    @State private var choiceID = ""
    @State private var effort = ""
    @State private var search = ""
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private var groups: [String] {
        var seen = Set<String>()
        return model.models.compactMap { seen.insert($0.connectionID).inserted ? $0.connectionID : nil }
    }
    private var choice: ChatRoute? { model.models.first { $0.id == choiceID } }
    private var representative: ChatRoute? { model.models.first { $0.connectionID == provider } }
    private var visible: [ChatRoute] {
        model.models.filter { route in
            (search.isEmpty ? route.connectionID == provider : true) &&
            (search.isEmpty || (route.label + " " + route.connectionLabel + " " + route.accountLabel).localizedCaseInsensitiveContains(search))
        }
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 7) {
                Image(systemName: "magnifyingglass").foregroundStyle(.secondary)
                TextField("Find an enabled model", text: $search).textFieldStyle(.plain)
                    .accessibilityLabel("Find an enabled model")
                Button { model.refresh() } label: { Image(systemName: "arrow.clockwise") }
                    .buttonStyle(.plain).help("Refresh enabled models from Provider Hub")
                    .accessibilityLabel("Refresh enabled models")
            }.font(.system(size: 12)).padding(.horizontal, 14).padding(.vertical, 12)
            Divider()
            HStack(spacing: 0) {
                providerRail.frame(width: 64)
                Divider()
                modelList.frame(maxWidth: .infinity)
                Divider()
                reasoning.frame(width: 120)
            }
            Divider()
            HStack {
                Text("Enabled in Provider Hub").font(.system(size: 10.5)).foregroundStyle(.secondary)
                Spacer()
                Button("Use model", action: commit).controlSize(.small)
                    .buttonStyle(.borderedProminent).tint(choice?.accent ?? HubTheme.Accent.brand)
                    .disabled(choice == nil || model.busy).keyboardShortcut(.defaultAction)
            }.padding(.horizontal, 14).padding(.vertical, 12)
        }
        .padding(8)
        .frame(width: 520, height: 388)
        .onAppear {
            let selected = model.selectedRoute ?? model.models.first
            provider = selected?.connectionID ?? ""; choiceID = selected?.id ?? ""
            effort = model.selected?.effort ?? ""
        }
        .onChange(of: model.models.map(\.id)) { _, ids in
            if !ids.contains(choiceID) {
                let first = model.models.first; provider = first?.connectionID ?? ""
                choiceID = first?.id ?? ""; effort = ""
            }
        }
        .onExitCommand(perform: dismiss)
    }

    private var providerRail: some View {
        ScrollView(showsIndicators: false) {
            VStack(spacing: 6) {
                ForEach(groups, id: \.self) { group in
                    if let route = model.models.first(where: { $0.connectionID == group }) {
                        Button {
                            provider = group; search = ""
                        } label: {
                            Group {
                                if let presentation = route.connectionPresentation ?? route.presentation {
                                    ProviderMark(presentation: presentation, size: 24)
                                } else {
                                    Text(String(route.connectionLabel.prefix(2))).font(.system(size: 10, weight: .semibold))
                                }
                            }
                            .frame(width: 40, height: 36)
                            .background(RoundedRectangle(cornerRadius: 7).fill(provider == group ? route.connectionAccent.opacity(0.16) : .clear))
                            .contentShape(Rectangle())
                        }.buttonStyle(.plain)
                            .help(route.connectionLabel).accessibilityLabel(route.connectionLabel)
                            .accessibilityAddTraits(provider == group ? .isSelected : [])
                    }
                }
            }.frame(maxWidth: .infinity).padding(.vertical, 10)
        }
    }

    private var modelList: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(search.isEmpty ? (representative?.connectionLabel ?? "Models") : "Matches")
                .font(.system(size: 11, weight: .semibold)).foregroundStyle(.secondary)
                .padding(.horizontal, 14).padding(.top, 12)
            ScrollView {
                LazyVStack(spacing: 2) {
                    ForEach(visible) { route in modelRow(route) }
                    if visible.isEmpty {
                        Text(search.isEmpty ? "Tick models in Provider Hub to list them here." : "No matching enabled models.")
                            .font(.system(size: 12)).foregroundStyle(.secondary)
                            .padding(12).frame(maxWidth: .infinity, alignment: .leading)
                    }
                }.padding(.horizontal, 8).padding(.vertical, 6)
            }
        }
    }

    private func modelRow(_ route: ChatRoute) -> some View {
        Button {
            choiceID = route.id
            if !route.efforts.contains(effort) { effort = "" }
        } label: {
            HStack(spacing: 7) {
                if let presentation = route.presentation {
                    ProviderMark(presentation: presentation, size: 14)
                } else {
                    Image(systemName: "sparkle").font(.system(size: 12)).foregroundStyle(.secondary)
                }
                VStack(alignment: .leading, spacing: 2) {
                    Text(route.label).font(.system(size: 12, weight: choiceID == route.id ? .medium : .regular))
                        .foregroundStyle(.primary).lineLimit(2)
                    Text(route.accountLabel + (route.context.map { " · \($0 / 1000)K" } ?? ""))
                        .font(.system(size: 10)).foregroundStyle(.secondary)
                }
                Spacer(minLength: 2)
                if choiceID == route.id { Image(systemName: "checkmark").font(.system(size: 10, weight: .semibold)).foregroundStyle(route.accent) }
            }
            .padding(.horizontal, 8).padding(.vertical, 7)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(RoundedRectangle(cornerRadius: 6).fill(choiceID == route.id ? route.accent.opacity(0.1) : .clear))
            .contentShape(Rectangle())
        }.buttonStyle(.plain)
            .accessibilityLabel(route.label + ", " + route.accountLabel)
            .accessibilityAddTraits(choiceID == route.id ? .isSelected : [])
    }

    private var reasoning: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Reasoning").font(.system(size: 11, weight: .semibold)).foregroundStyle(.secondary)
            if let choice, !choice.efforts.isEmpty {
                ScrollView {
                    VStack(alignment: .leading, spacing: 3) {
                        ForEach(Array(choice.efforts.reversed()), id: \.self) { value in effortRow(value, accent: choice.accent) }
                        effortRow("", accent: choice.accent)
                    }
                }
            } else {
                Text("Provider default").font(.system(size: 11)).foregroundStyle(.secondary)
                Spacer()
            }
        }.padding(.horizontal, 10).padding(.vertical, 10)
            .frame(maxHeight: .infinity, alignment: .top)
    }

    private func effortRow(_ value: String, accent: Color) -> some View {
        let title = value.isEmpty ? "Default" : value == "xhigh" ? "Extra high" : value.capitalized
        return Button {
            withAnimation(reduceMotion ? nil : .easeInOut(duration: 0.12)) { effort = value }
        } label: {
            HStack(spacing: 7) {
                Circle().fill(effort == value ? accent : Color.secondary.opacity(0.3)).frame(width: 6, height: 6)
                Text(title).font(.system(size: 11, weight: effort == value ? .semibold : .regular))
                    .foregroundStyle(effort == value ? Color.primary : .secondary)
            }.frame(maxWidth: .infinity, alignment: .leading).padding(.vertical, 5).contentShape(Rectangle())
        }.buttonStyle(.plain).accessibilityLabel("Reasoning: " + title)
            .accessibilityAddTraits(effort == value ? .isSelected : [])
    }

    private func commit() {
        guard let choice else { return }
        if choice.id == model.selectedRoute?.id && choice.scope == model.selected?.scope { model.setEffort(effort) }
        else { model.setSelection(choice.id, effort: effort) }
        dismiss()
    }
}
