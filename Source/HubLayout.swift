import SwiftUI

enum HubClientSelection: String, CaseIterable, Identifiable {
    case both = "Both apps", codex = "Codex / ChatGPT", claude = "Claude"
    var id: String { rawValue }
}

/// Both workspaces keep the same client order and fall back to one pane when
/// the window cannot give each catalogue a useful editing width.
struct HubClientWorkspace: View {
    @ObservedObject var model: BridgeModel
    @Binding var selection: HubClientSelection
    var allowsSplit: Bool
    var configuration: Bool

    private var effectiveSelection: HubClientSelection {
        selection == .both && !allowsSplit ? .codex : selection
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            Picker("Desktop app", selection: Binding(
                get: { effectiveSelection }, set: { selection = $0 }
            )) {
                if allowsSplit { Text("Both apps").tag(HubClientSelection.both) }
                Text("Codex / ChatGPT").tag(HubClientSelection.codex)
                Text("Claude").tag(HubClientSelection.claude)
            }.pickerStyle(.segmented).labelsHidden().frame(maxWidth: 400)

            if configuration {
                Panel {
                    Text("Shared gateway").font(.headline)
                    Toggle(isOn: $model.settings.auto_stop) {
                        VStack(alignment: .leading, spacing: 4) {
                            Text("Stop gateway after the desktop sessions close").font(.system(size: 13, weight: .medium))
                            Text("Applies to both apps. The menu bar app stays available.").font(.caption).foregroundStyle(.secondary)
                        }.frame(maxWidth: .infinity, alignment: .leading)
                    }.toggleStyle(.switch)
                }
            } else {
                HStack(spacing: 12) {
                    Text(model.catalogueSummary).font(.system(size: 11)).foregroundStyle(.secondary)
                        .frame(maxWidth: .infinity, alignment: .leading)
                    if model.catalogueRefreshing { ProgressView().controlSize(.small) }
                    Button("Refresh all") { model.beginCatalogueRefresh(userInitiated: true) }
                        .disabled(model.busy || model.catalogueRefreshing)
                    Button("Providers") { model.page = .connection }
                }
            }

            HStack(alignment: .top, spacing: 18) {
                if effectiveSelection != .claude {
                    Group {
                        if configuration { CodexConfigPane(model: model) }
                        else { CodexModelsPane(model: model) }
                    }.frame(maxWidth: .infinity, alignment: .topLeading)
                }
                if effectiveSelection != .codex {
                    Group {
                        if configuration { ClaudeConfigPane(model: model) }
                        else { ClaudeModelsPane(model: model) }
                    }.frame(maxWidth: .infinity, alignment: .topLeading)
                }
            }
        }
    }
}

struct HubPaneHeading: View {
    let title: String
    let subtitle: String
    let icon: String
    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: icon).font(.system(size: 20)).foregroundStyle(.secondary)
                .frame(width: 34, height: 34).background(Color.white.opacity(0.05), in: RoundedRectangle(cornerRadius: 8))
            VStack(alignment: .leading, spacing: 4) {
                Text(title).font(.system(size: 18, weight: .semibold))
                Text(subtitle).font(.caption).foregroundStyle(.secondary)
            }
        }
    }
}

struct ClaudeModelsPane: View {
    @ObservedObject var model: BridgeModel
    @State private var search = ""

    private var filteredCatalogue: [ClaudeCatalogueEntry] {
        let query = search.trimmingCharacters(in: .whitespacesAndNewlines)
        return query.isEmpty ? model.claudeCatalogue : model.claudeCatalogue.filter { model.modelLabel($0.route).localizedCaseInsensitiveContains(query) || $0.route.localizedCaseInsensitiveContains(query) }
    }

    var body: some View {
        Panel {
            HubPaneHeading(title: "Claude Desktop", subtitle: "Curated catalogue", icon: "macwindow")
            HStack {
                Text("\(model.claudeCatalogue.count) models in Claude’s picker").font(.caption).foregroundStyle(.secondary)
                Spacer()
                addMenu
            }
            TextField("Find a model or provider", text: $search).textFieldStyle(.roundedBorder)
                .accessibilityLabel("Search Claude catalogue")
            if model.settings.claude_catalogue == nil {
                Text("Showing your existing mappings. Editing this list creates a curated catalogue; the original mappings remain saved.")
                    .font(.caption).foregroundStyle(.secondary)
                if model.settings.mapping_options.values.contains(where: { $0.omit_system || $0.omit_tools }) {
                    Text("Legacy slots omit system or tool fields. Curated models send both fields; the original slot options remain saved.")
                        .font(.caption).foregroundStyle(.orange)
                }
            }
            if model.claudeCatalogue.isEmpty {
                Text("Connect a provider, refresh its models, then add models here.").font(.callout).foregroundStyle(.secondary)
            }
            if !model.claudeCatalogue.isEmpty && filteredCatalogue.isEmpty {
                Text("No models match your search.").font(.callout).foregroundStyle(.secondary)
            }
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 7) {
                    ForEach(filteredCatalogue) { entry in
                        ClaudeCatalogueRow(model: model, entry: entry)
                    }
                }.padding(.trailing, 4)
            }.frame(height: 360).accessibilityLabel("Claude model catalogue")
            HStack(spacing: 12) {
                Button("Add all") { model.addAllClaudeRoutes() }.disabled(model.availableModels.isEmpty)
                Spacer()
                Toggle("Technical IDs", isOn: $model.showRoutingIDs).toggleStyle(.checkbox).font(.caption)
            }
            Divider()
            VStack(alignment: .leading, spacing: 8) {
                Text("Family defaults").font(.system(size: 13, weight: .semibold))
                ForEach(claudeTiers, id: \.id) { tier in
                    if let selected = model.claudeCatalogue.first(where: { $0.tier == tier.id && model.isClaudeTierDefault($0) }) {
                        HStack(alignment: .firstTextBaseline) {
                            Text(tier.label).font(.system(size: 12, weight: .medium)).frame(width: 52, alignment: .leading)
                            Text(model.modelLabel(selected.route)).font(.system(size: 12)).foregroundStyle(.secondary)
                                .frame(maxWidth: .infinity, alignment: .leading)
                        }
                    }
                }
                Text("Claude starts with the Fable default, or Opus when Fable is absent.").font(.caption).foregroundStyle(.secondary)
            }
            DisclosureGroup("Family, effort and context") {
                VStack(alignment: .leading, spacing: 8) {
                    Text("A family’s default also answers Claude Code’s requests for that family. Missing families use the nearest available tier.")
                    Text("Fable, Opus and Sonnet offer Claude’s effort ladder, including Ultracode. Haiku has no effort control. Enable catalogue models and workflows in Config.")
                    Text("Context follows provider metadata. A blank compaction threshold uses 85% of the known catalogue window. Unknown windows remain provider-managed.")
                    Text("Fast uses a provider’s same-model capability; it never silently changes the selected model.")
                }.font(.caption).foregroundStyle(.secondary).padding(.top, 8)
            }.font(.caption)
        }
    }

    private var addMenu: some View {
        Menu {
            ForEach(model.providerDefinitions) { provider in
                let options = model.availableModels.filter { candidate in
                    candidate.provider_id == provider.id && (candidate.tools ?? true)
                        && !model.claudeCatalogue.contains { $0.route == candidate.id }
                }
                if !options.isEmpty {
                    Menu(provider.presentation.displayProvider) {
                        ForEach(options) { entry in
                            Button("\(model.modelLabel(entry.id)) — \(model.modelFacts(entry.id))") { model.addClaudeRoute(entry.id) }
                                .help(entry.id)
                        }
                    }
                }
            }
        } label: { Label("Add", systemImage: "plus") }
        .fixedSize().disabled(model.busy || model.availableModels.isEmpty)
    }
}

struct ClaudeCatalogueRow: View {
    @ObservedObject var model: BridgeModel
    let entry: ClaudeCatalogueEntry
    @State private var expanded = false

    var body: some View {
        DisclosureGroup(isExpanded: $expanded) {
            VStack(alignment: .leading, spacing: 12) {
                Divider()
                Text(model.modelFacts(entry.route)).font(.system(size: 11)).foregroundStyle(.secondary)
                if model.modelEntry(entry.route) == nil {
                    Text("Not currently advertised. Refresh the provider or remove this selection.").font(.caption).foregroundStyle(.orange)
                }
                Picker("Claude family", selection: model.claudeTier(for: entry.route)) {
                    ForEach(claudeTiers, id: \.id) { tier in Text(tier.label).tag(tier.id) }
                }.font(.system(size: 12))
                VStack(alignment: .leading, spacing: 5) {
                    Text("Compact at · tokens").font(.system(size: 11)).foregroundStyle(.secondary)
                    TextField("Automatic · 85%", text: model.claudeCompactLimit(for: entry.route))
                        .textFieldStyle(.roundedBorder).font(.system(size: 12, design: .monospaced))
                        .accessibilityLabel("\(model.modelLabel(entry.route)) compaction threshold")
                    Text("Blank uses the automatic threshold. Use 1,000–15,000,000; values are capped at the model’s window.").font(.system(size: 11)).foregroundStyle(.secondary)
                }
                HStack(spacing: 10) {
                    Button(model.isClaudeTierDefault(entry) ? "Default for family" : "Set family default") { model.makeClaudeTierDefault(entry.route) }
                        .disabled(model.isClaudeTierDefault(entry))
                    Spacer(minLength: 0)
                    Button("Test") { Task { await model.testRoute(entry.route) } }
                }.font(.system(size: 11))
                Button(role: .destructive) { model.removeClaudeRoute(entry.route) } label: {
                    Label("Remove from catalogue", systemImage: "minus.circle")
                }.font(.system(size: 11)).disabled(model.claudeCatalogue.count <= 1)
            }.padding(.top, 8)
        } label: {
            HStack(alignment: .top, spacing: 8) {
                VStack(alignment: .leading, spacing: 4) {
                    Text(model.modelLabel(entry.route)).font(.system(size: 12, weight: .medium)).fixedSize(horizontal: false, vertical: true)
                    if model.showRoutingIDs { Text(entry.route).font(.system(size: 10, design: .monospaced)).foregroundStyle(.secondary).textSelection(.enabled) }
                    else { Text(contextLabel).font(.system(size: 11)).foregroundStyle(.secondary) }
                }.frame(maxWidth: .infinity, alignment: .leading)
                VStack(alignment: .trailing, spacing: 4) {
                    Text(claudeTiers.first { $0.id == entry.tier }?.label ?? entry.tier).font(.system(size: 11)).foregroundStyle(.secondary)
                    if model.isClaudeTierDefault(entry) {
                        Image(systemName: "star.fill").font(.system(size: 10)).foregroundStyle(.orange).accessibilityLabel("Family default")
                    }
                }
            }
        }.padding(11).background(Color.white.opacity(0.045), in: RoundedRectangle(cornerRadius: 8))
    }

    private var contextLabel: String {
        guard let item = model.modelEntry(entry.route) else { return "Not currently advertised" }
        return (item.runtime_context ?? item.context).map { "\($0.formatted()) tokens" } ?? "Provider-managed context"
    }
}

struct ClaudeConfigPane: View {
    @ObservedObject var model: BridgeModel

    var body: some View {
        Panel {
            HubPaneHeading(title: "Claude Desktop", subtitle: "App preferences", icon: "macwindow")
            VStack(alignment: .leading, spacing: 13) {
                Text("Permissions and features").font(.system(size: 13, weight: .semibold))
                Toggle(isOn: $model.settings.auto_mode) {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("Claude Auto mode")
                        Text("Claude chooses its approval reviewer internally.").font(.caption).foregroundStyle(.secondary)
                    }.frame(maxWidth: .infinity, alignment: .leading)
                }
                Divider()
                Toggle(isOn: $model.settings.claude_features.dictation) { Text("Dictation").frame(maxWidth: .infinity, alignment: .leading) }
                Toggle(isOn: $model.settings.claude_features.builtin_browser) { Text("Built-in browser").frame(maxWidth: .infinity, alignment: .leading) }
                Toggle(isOn: $model.settings.claude_features.claude_in_chrome) { Text("Claude in Chrome").frame(maxWidth: .infinity, alignment: .leading) }
                Toggle(isOn: $model.settings.claude_features.scheduled_tasks) { Text("Scheduled tasks").frame(maxWidth: .infinity, alignment: .leading) }
                Toggle(isOn: $model.settings.claude_features.cowork_tab) { Text("Cowork tab").frame(maxWidth: .infinity, alignment: .leading) }
                Text("These features apply at the next Claude launch.").font(.caption).foregroundStyle(.secondary)
            }.font(.system(size: 12)).toggleStyle(.switch).controlSize(.small)
            Divider()
            VStack(alignment: .leading, spacing: 13) {
                Text("Claude Code").font(.system(size: 13, weight: .semibold))
                Toggle(isOn: $model.settings.claude_code_settings) { Text("Use catalogue models in Claude Code").frame(maxWidth: .infinity, alignment: .leading) }
                Toggle(isOn: $model.settings.claude_workflows) { Text("Enable Ultracode workflows").frame(maxWidth: .infinity, alignment: .leading) }
                DisclosureGroup("Compatibility details") {
                    VStack(alignment: .leading, spacing: 9) {
                        Text("Features depend on the installed Claude build supporting them in third-party profiles. Dictation and scheduled tasks can remain hidden on unsupported builds.")
                        Text("Catalogue models add temporary model-picker entries to Claude Code settings so each model gets its family’s effort ladder and capabilities. Terminal Claude Code sees those entries while this profile is active.")
                        Text("Ultracode workflows require the Fable, Opus or Sonnet family. They enable Claude’s dynamic workflows, use the provider’s highest reasoning setting, and ask the model to use the Workflow tool.")
                        Text("Claude keeps first-party and third-party histories separately. This profile shares Claude’s third-party store with Ollama; existing conversations are retained.")
                    }.font(.caption).foregroundStyle(.secondary).padding(.top, 8)
                }.font(.caption)
            }.font(.system(size: 12)).toggleStyle(.switch).controlSize(.small)
            Divider()
            VStack(alignment: .leading, spacing: 10) {
                HStack {
                    Text("Provider profile").font(.system(size: 12, weight: .medium))
                    Spacer()
                    Text(model.profileActive ? "Active" : "Ready to launch").font(.caption).foregroundStyle(.secondary)
                }
                if model.claudeRunning && !model.profileActive {
                    Text("Claude is open with another profile. Quit Claude before switching to this one.").font(.caption).foregroundStyle(.orange)
                }
                Button(model.profileActive && model.claudeRunning ? "Show Claude" : "Launch Claude") {
                    Task { await model.launchClaude() }
                }.buttonStyle(.borderedProminent).disabled(model.busy || !model.claudeInstalled)
                if model.recoveryNeeded {
                    Button("Restore previous setup") { Task { await model.restore() } }.disabled(model.busy || model.claudeRunning)
                }
                Text("The previous setup is restored after Claude quits.").font(.caption).foregroundStyle(.secondary)
                if model.codexRunning && model.codexRecoveryNeeded {
                    Text("Codex / ChatGPT shares this gateway. Applying changed model routes may briefly reconnect it.").font(.caption).foregroundStyle(.secondary)
                }
            }
        }
    }
}
