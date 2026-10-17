import SwiftUI

protocol ChatPickerChoice: Identifiable where ID == String {
    var label: String { get }
    var accountLabel: String { get }
    var context: Int? { get }
    var efforts: [String] { get }
    var connectionID: String { get }
    var connectionLabel: String { get }
    var accent: Color { get }
    var connectionAccent: Color { get }
}

/// The desktop's combined model picker, shared without host/model dependencies.
/// Only presentation adapts: the reasoning sidecar becomes a bottom rail below
/// 460 points, retaining the same choice/effort selection and commit semantics.
struct ChatSharedModelPicker<Choice: ChatPickerChoice, Mark: View>: View {
    var choices: [Choice]
    var initialChoiceID: String?
    var initialEffort: String
    var disabled = false
    var refresh: () -> Void
    var choose: (String, String) -> Void
    @ViewBuilder var mark: (Choice, Bool, CGFloat) -> Mark
    @State private var provider = ""
    @State private var choiceID = ""
    @State private var effort = ""
    @State private var search = ""
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private var choice: Choice? { choices.first { $0.id == choiceID } }
    private var groups: [String] {
        var seen = Set<String>()
        return choices.compactMap { seen.insert($0.connectionID).inserted ? $0.connectionID : nil }
    }
    private var visible: [Choice] {
        choices.filter { row in (search.isEmpty ? row.connectionID == provider : true) &&
            (search.isEmpty || (row.label + " " + row.connectionLabel + " " + row.accountLabel).localizedCaseInsensitiveContains(search)) }
    }
    var body: some View {
        GeometryReader { geometry in
            let compact = geometry.size.width < 460
            VStack(spacing: 0) {
                HStack(spacing: 7) {
                    Image(systemName: "magnifyingglass").foregroundStyle(.secondary)
                    TextField("Find an enabled model", text: $search).textFieldStyle(.plain).accessibilityLabel("Find an enabled model")
                    Button(action: refresh) { Image(systemName: "arrow.clockwise") }.buttonStyle(.plain)
                        .accessibilityLabel("Refresh enabled models").disabled(disabled)
                }.font(.system(size: 12)).padding(.horizontal, 14).padding(.vertical, 12)
                Divider()
                HStack(spacing: 0) {
                    providerRail.frame(width: compact ? 56 : 64)
                    Divider()
                    modelList.frame(maxWidth: .infinity)
                    if !compact { Divider(); reasoning.frame(width: 120) }
                }
                if compact {
                    Divider()
                    VStack(alignment: .leading, spacing: 6) {
                        Text("Reasoning").font(.system(size: 11, weight: .semibold)).foregroundStyle(.secondary)
                        ScrollView(.horizontal, showsIndicators: false) {
                            HStack(spacing: 16) {
                                if let choice { ForEach(Array(choice.efforts.reversed()) + [""], id: \.self) { effortRow($0, accent: choice.accent) } }
                            }
                        }
                    }.padding(.horizontal, 14).padding(.vertical, 8)
                }
                Divider()
                HStack {
                    Text("Enabled in Provider Hub").font(.system(size: 10.5)).foregroundStyle(.secondary)
                    Spacer()
                    Button("Use model") { if let choice { choose(choice.id, effort) } }.controlSize(.small)
                        .buttonStyle(.borderedProminent).tint(choice?.accent ?? .accentColor)
                        .disabled(choice == nil || disabled).keyboardShortcut(.defaultAction)
                }.padding(.horizontal, 14).padding(.vertical, 12)
            }.padding(8)
        }
        .onAppear { selectInitial() }
        .onChange(of: choices.map(\.id)) { _, ids in if !ids.contains(choiceID) { selectInitial() } }
    }
    private func selectInitial() {
        let row = choices.first { $0.id == initialChoiceID } ?? choices.first
        choiceID = row?.id ?? ""; provider = row?.connectionID ?? ""
        effort = row?.efforts.contains(initialEffort) == true ? initialEffort : ""
    }
    private var providerRail: some View {
        ScrollView(showsIndicators: false) {
            VStack(spacing: 6) {
                ForEach(groups, id: \.self) { group in
                    if let row = choices.first(where: { $0.connectionID == group }) {
                        Button { provider = group; search = "" } label: {
                            mark(row, true, 24).frame(width: 40, height: 36)
                                .background(RoundedRectangle(cornerRadius: 7).fill(provider == group ? row.connectionAccent.opacity(0.16) : .clear))
                                .contentShape(Rectangle())
                        }.buttonStyle(.plain).help(row.connectionLabel).accessibilityLabel(row.connectionLabel)
                            .accessibilityAddTraits(provider == group ? .isSelected : [])
                    }
                }
            }.frame(maxWidth: .infinity).padding(.vertical, 10)
        }
    }
    private var modelList: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(search.isEmpty ? choices.first(where: { $0.connectionID == provider })?.connectionLabel ?? "Models" : "Matches")
                .font(.system(size: 11, weight: .semibold)).foregroundStyle(.secondary).padding(.horizontal, 14).padding(.top, 12)
            ScrollView {
                LazyVStack(spacing: 2) {
                    ForEach(visible) { row in
                        Button { choiceID = row.id; if !row.efforts.contains(effort) { effort = "" } } label: {
                            HStack(spacing: 7) {
                                mark(row, false, 14)
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(row.label).font(.system(size: 12, weight: choiceID == row.id ? .medium : .regular)).foregroundStyle(.primary).lineLimit(2)
                                    Text(row.accountLabel + (row.context.map { " · \($0 / 1000)K" } ?? "")).font(.system(size: 10)).foregroundStyle(.secondary)
                                }
                                Spacer(minLength: 2)
                                if choiceID == row.id { Image(systemName: "checkmark").font(.system(size: 10, weight: .semibold)).foregroundStyle(row.accent) }
                            }.padding(.horizontal, 8).padding(.vertical, 7).frame(maxWidth: .infinity, alignment: .leading)
                                .background(RoundedRectangle(cornerRadius: 6).fill(choiceID == row.id ? row.accent.opacity(0.1) : .clear)).contentShape(Rectangle())
                        }.buttonStyle(.plain).accessibilityLabel(row.label + ", " + row.accountLabel).accessibilityAddTraits(choiceID == row.id ? .isSelected : [])
                    }
                    if visible.isEmpty { Text(search.isEmpty ? "Tick models in Provider Hub to list them here." : "No matching enabled models.").font(.system(size: 12)).foregroundStyle(.secondary).padding(12) }
                }.padding(.horizontal, 8).padding(.vertical, 6)
            }
        }
    }
    private var reasoning: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Reasoning").font(.system(size: 11, weight: .semibold)).foregroundStyle(.secondary)
            if let choice, !choice.efforts.isEmpty {
                ScrollView { VStack(alignment: .leading, spacing: 3) { ForEach(Array(choice.efforts.reversed()) + [""], id: \.self) { effortRow($0, accent: choice.accent) } } }
            } else { Text("Provider default").font(.system(size: 11)).foregroundStyle(.secondary); Spacer() }
        }.padding(.horizontal, 10).padding(.vertical, 10).frame(maxHeight: .infinity, alignment: .top)
    }
    private func effortRow(_ value: String, accent: Color) -> some View {
        let title = ChatSharedEffort.title(value)
        return Button { withAnimation(reduceMotion ? nil : .easeInOut(duration: 0.12)) { effort = value } } label: {
            HStack(spacing: 7) {
                Circle().fill(effort == value ? accent : Color.secondary.opacity(0.3)).frame(width: 6, height: 6)
                Text(title).font(.system(size: 11, weight: effort == value ? .semibold : .regular)).foregroundStyle(effort == value ? Color.primary : .secondary)
            }.frame(maxWidth: .infinity, alignment: .leading).padding(.vertical, 5).contentShape(Rectangle())
        }.buttonStyle(.plain).accessibilityLabel("Reasoning: " + title).accessibilityAddTraits(effort == value ? .isSelected : [])
    }
}

/// A reasoning effort as both shells name it: "Extra high" for xhigh,
/// "Default" for the provider's own choice.
enum ChatSharedEffort {
    static func title(_ effort: String) -> String {
        if effort.isEmpty { return "Default" }
        return effort.lowercased() == "xhigh" ? "Extra high" : effort.prefix(1).uppercased() + effort.dropFirst()
    }
}
