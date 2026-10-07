import AppKit
import SwiftUI

// The compact design: one glass window whose left rail lists providers,
// subscriptions and the two desktop apps, a middle pane for whichever tab is
// selected, and a right column for status, actions and the selected model's
// settings. It binds to the same BridgeModel as the classic pages, so both
// designs edit the same unsaved settings and either can save or launch.
// Spec: Source/HubUI/mockups/provider-hub-shell-v3.template.html.

enum ShellTab: Hashable { case provider(String), claude, codex, settings }

enum Chroma {
    static let one = Color(red: 0x5a / 255, green: 0x8c / 255, blue: 0xff / 255)
    static let two = Color(red: 0xbf / 255, green: 0x7c / 255, blue: 0xff / 255)
    static let three = Color(red: 0x41 / 255, green: 0xc7 / 255, blue: 0xe5 / 255)
    static let claude = Color(red: 0xd9 / 255, green: 0x7b / 255, blue: 0x3a / 255)
    static let codex = Color(red: 0x70 / 255, green: 0x5a / 255, blue: 0xff / 255)
    static let ok = Color(red: 0x3e / 255, green: 0xcf / 255, blue: 0x8e / 255)
    static let warn = Color(red: 0xf5 / 255, green: 0xb6 / 255, blue: 0x42 / 255)
    static let bad = Color(red: 1, green: 0x5d / 255, blue: 0x5d / 255)
    static let tiers: [String: Color] = ["fable": two, "opus": HubTheme.Accent.brand, "sonnet": one, "haiku": three]
}

/// Window vibrancy behind the shell: the desktop shows through the glass.
/// The material follows the effective appearance (HubTheme.Appearance): HUD
/// glass on dark, popover glass on light, so the window re-tints on a change
/// without any SwiftUI state.
struct VibrancyBackground: NSViewRepresentable {
    final class AppearanceGlass: NSVisualEffectView {
        override func viewDidChangeEffectiveAppearance() {
            super.viewDidChangeEffectiveAppearance()
            material = effectiveAppearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua ? .hudWindow : .popover
        }
    }
    func makeNSView(context: Context) -> NSVisualEffectView {
        let view = AppearanceGlass()
        view.blendingMode = .behindWindow; view.state = .active
        view.viewDidChangeEffectiveAppearance()
        return view
    }
    func updateNSView(_ view: NSVisualEffectView, context: Context) {}
}

/// Shorthands for the semantic colours the shell lays over the glass.
private typealias Semantic = HubTheme.Semantic

private let subscriptionProviders: Set<String> = ["claude", "codex", "antigravity", "devin"]
private let cliAccountProviders: Set<String> = ["codex", "claude"]
private let tierOrder = ["fable", "opus", "sonnet", "haiku"]

// MARK: - Model helpers the shell needs that the classic panes keep in views

extension BridgeModel {
    var codexCuratedRoutes: [String] {
        if let curated = settings.codex_catalogue { return curated }
        var routes = codexModels.map(\.id)
        if let starting = settings.codex_model, !routes.contains(starting) { routes.append(starting) }
        return routes
    }
    func codexMaterialise() {
        if settings.codex_catalogue == nil && !codexCuratedRoutes.isEmpty { settings.codex_catalogue = codexCuratedRoutes }
    }
    private func codexPrune(to routes: [String]) {
        let kept = (settings.codex_subagent_rank ?? [:]).filter { routes.contains($0.key) }
        settings.codex_subagent_rank = kept.isEmpty ? nil : kept
        if let route = settings.codex_subagent_route, !routes.contains(route) { settings.codex_subagent_route = nil }
    }
    func codexSetListed(_ route: String, _ listed: Bool) {
        if listed {
            guard !codexCuratedRoutes.contains(route) else { return }
            settings.codex_catalogue = codexCuratedRoutes + [route]
            if settings.codex_model == nil { settings.codex_model = route }
        } else {
            guard codexCuratedRoutes.count > 1 else { return }
            let list = codexCuratedRoutes.filter { $0 != route }
            settings.codex_catalogue = list
            if settings.codex_model == route { settings.codex_model = list.first }
            codexPrune(to: list)
        }
    }
    func codexSetRank(_ route: String, _ rank: Int?) {
        codexMaterialise()
        var ranks = settings.codex_subagent_rank ?? [:]
        if let rank { ranks[route] = rank } else { ranks.removeValue(forKey: route) }
        settings.codex_subagent_rank = ranks.isEmpty ? nil : ranks
    }
    /// "all" lists every compatible model, "custom" is the curated list.
    var codexMode: String { settings.codex_catalogue == nil ? "all" : "custom" }
    func setCodexMode(_ mode: String) {
        if mode == "all" { settings.codex_catalogue = nil } else { codexMaterialise() }
    }
    /// "slots" keeps the five mappings, "all" lists every compatible model, "curate" anything else.
    var claudeMode: String {
        guard settings.claude_catalogue != nil else { return "slots" }
        let listed = Set(claudeCatalogue.map(\.route))
        let compatible = availableModels.filter { $0.tools ?? true }.map(\.id)
        return !compatible.isEmpty && compatible.allSatisfy { listed.contains($0) } ? "all" : "curate"
    }
    func setClaudeMode(_ mode: String) {
        switch mode {
        case "slots": settings.claude_catalogue = nil
        case "all": if settings.claude_catalogue == nil { settings.claude_catalogue = claudeCatalogue }; addAllClaudeRoutes()
        default: if settings.claude_catalogue == nil { settings.claude_catalogue = claudeCatalogue }
        }
    }
    func claudeEntry(_ route: String) -> ClaudeCatalogueEntry? {
        settings.claude_catalogue == nil ? nil : claudeCatalogue.first { $0.route == route }
    }
    func shellProviderState(_ id: String) -> (text: String, color: Color, on: Bool) {
        if id == "ollama" { return ("Local daemon", Chroma.ok, true) }
        let found = providerStates[id]?.credential_found == true
        let cli = settings.providers[id]?.credential_mode == "cli"
        if found { return (cli ? "Signed in" : "Connected", Chroma.ok, true) }
        return (cli ? "Not signed in" : "Needs key", cli ? Color.secondary : Chroma.warn, false)
    }
    func providerModels(_ id: String) -> [ModelEntry] { availableModels.filter { $0.provider_id == id } }
    func contextLabel(_ entry: ModelEntry) -> String {
        guard let tokens = entry.runtime_context ?? entry.context, tokens > 0 else { return "provider-managed" }
        return tokens >= 1_000_000 ? "\(tokens / 1_000_000)M" : "\(tokens / 1000)K"
    }
}

// MARK: - Shell

struct CompactShell: View {
    @ObservedObject var model: BridgeModel
    @State private var tab: ShellTab = .provider("mistral")
    @State private var railOpen = false
    @State private var focusedRoute: String?
    @State private var search = ""
    @State private var showAudit = false
    @State private var tabBeforeSettings: ShellTab = .provider("mistral")
    @Namespace private var rail

    var apiProviders: [ProviderDefinition] { model.providerDefinitions.filter { !subscriptionProviders.contains($0.id) } }
    var cliProviders: [ProviderDefinition] { model.providerDefinitions.filter { subscriptionProviders.contains($0.id) } }
    var auditAllowed: Bool { UserDefaults.standard.bool(forKey: "hubShowAudit") }

    var body: some View {
        VStack(spacing: 0) {
            titleBar
            if tab == .settings {
                SettingsPage(model: model)
            } else {
                HStack(spacing: 0) {
                    railView.frame(width: railOpen ? 176 : 52)
                    mainPane.frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
                    sidePane.frame(width: 196)
                }
            }
        }
        .font(.system(size: 11.5))
        .background(VibrancyBackground())
        .background(chromaWash)
        .frame(minWidth: 760, minHeight: 500)
        // The hosting view insets content below the hidden title bar; the glass
        // has to reach the traffic lights, so the whole shell owns that band.
        .ignoresSafeArea()
        .sheet(isPresented: $showAudit) { auditSheet }
        .onAppear { if model.providerDefinitions.contains(where: { $0.id == model.selectedProvider }) { tab = .provider(model.selectedProvider) } }
        .onChange(of: tab) { _, new in if case .provider(let id) = new { model.selectedProvider = id; model.secretDraft = "" }; if new != .settings { search = ""; focusedRoute = nil } }
        .accentColor(HubTheme.Accent.brand)
    }

    @Environment(\.colorScheme) private var colorScheme
    private var chromaWash: some View {
        let strength = colorScheme == .dark ? 1.0 : 0.45
        return ZStack {
            RadialGradient(colors: [Chroma.one.opacity(0.22 * strength), .clear], center: UnitPoint(x: 0.15, y: 0.05), startRadius: 0, endRadius: 520)
            RadialGradient(colors: [Chroma.two.opacity(0.18 * strength), .clear], center: UnitPoint(x: 0.9, y: 0.15), startRadius: 0, endRadius: 460)
            RadialGradient(colors: [Chroma.three.opacity(0.12 * strength), .clear], center: UnitPoint(x: 0.55, y: 1.0), startRadius: 0, endRadius: 420)
        }.allowsHitTesting(false)
    }

    // MARK: title bar

    private var titleBar: some View {
        HStack(spacing: 8) {
            Image(nsImage: NSApp.applicationIconImage).resizable().frame(width: 18, height: 18).clipShape(RoundedRectangle(cornerRadius: 5))
            Text("Provider Hub").font(.system(size: 12, weight: .semibold))
            Spacer()
            Circle().fill(model.running ? Chroma.ok : Color.secondary.opacity(0.5)).frame(width: 7, height: 7)
                .shadow(color: model.running ? Chroma.ok.opacity(0.5) : .clear, radius: 3)
            Text("127.0.0.1:\(String(model.savedSettings.port))").font(.system(size: 10.5, design: .monospaced)).foregroundStyle(.secondary)
            Button(model.running ? "Stop" : "Start") { Task { await model.toggleGateway() } }
                .buttonStyle(.bordered).controlSize(.mini).disabled(model.busy)
            launchCluster.padding(.leading, 10)
            if model.changed {
                Button { Task { await model.saveFromUI() } } label: { Text("Save").foregroundStyle(HubTheme.Control.ink) }
                    .buttonStyle(.borderedProminent).controlSize(.mini).tint(HubTheme.Accent.brand)
                    .disabled(model.busy || model.settingsSaveBlocker != nil).help(model.settingsSaveBlocker ?? "Save unsaved changes")
                    .padding(.leading, 6)
            }
            if auditAllowed {
                Button { showAudit = true } label: { Image(systemName: "list.bullet.rectangle") }.buttonStyle(.borderless).help("Audit")
            }
            Button {
                if tab == .settings { tab = tabBeforeSettings } else { tabBeforeSettings = tab; tab = .settings }
            } label: {
                Image(systemName: "gearshape.fill").font(.system(size: 12)).foregroundStyle(tab == .settings ? Color.primary : Color.secondary)
                    .frame(width: 22, height: 22).background(Circle().fill(tab == .settings ? Semantic.selection : Color.clear))
                    .contentShape(Circle())
            }.buttonStyle(.plain).help(tab == .settings ? "Back" : "Settings").padding(.leading, 4)
            if model.busy { ProgressView().controlSize(.mini) }
        }
        .padding(.leading, 78).padding(.trailing, 12).frame(height: 36)
    }

    /// "Launch" plus the two app marks, each with its own LED: green while
    /// the app runs on the hub profile, amber when it runs on another one,
    /// grey when closed. One click launches (or shows) the app with the
    /// current catalogue, same as the app tab's button.
    private var launchCluster: some View {
        HStack(spacing: 4) {
            Text("Launch").font(.system(size: 10.5)).foregroundStyle(.secondary).padding(.trailing, 2)
            launchMark(.claude)
            launchMark(.codex)
        }
    }

    private func launchMark(_ which: ShellTab) -> some View {
        let isClaude = which == .claude
        let presentation = model.providerDefinitions.first { $0.id == (isClaude ? "claude" : "codex") }?.presentation
        let running = isClaude ? model.claudeRunning : model.codexRunning
        let live = running && (isClaude ? model.profileActive : model.codexProfileActive)
        let led: Color = live ? Chroma.ok : (running ? Chroma.warn : Color.secondary.opacity(0.5))
        let blocked = model.busy || (isClaude ? !model.claudeInstalled : (model.codexAppPath == nil || model.settings.codex_model == nil))
        return Button { Task { if isClaude { await model.launchClaude() } else { await model.launchCodex() } } } label: {
            ZStack(alignment: .bottomTrailing) {
                Group {
                    if let presentation { ProviderMark(presentation: presentation, size: 16) }
                    else { Image(systemName: "app.dashed").font(.system(size: 12)) }
                }.frame(width: 22, height: 22)
                Circle().fill(led).frame(width: 6, height: 6).overlay(Circle().stroke(Semantic.surface, lineWidth: 1))
                    .shadow(color: live ? Chroma.ok.opacity(0.6) : .clear, radius: 2).offset(x: 1, y: 1)
            }
            .background(RoundedRectangle(cornerRadius: 6).fill(Semantic.raisedSurface))
            .contentShape(RoundedRectangle(cornerRadius: 6))
            .opacity(blocked ? 0.45 : 1)
        }.buttonStyle(.plain).disabled(blocked)
            .help((isClaude ? "Claude Desktop" : "Codex / ChatGPT") + (live ? " · running on the hub profile" : (running ? " · running on another profile" : " · closed")))
    }

    // MARK: rail

    private var railView: some View {
        VStack(alignment: railOpen ? .leading : .center, spacing: 3) {
            ScrollView(showsIndicators: false) {
                VStack(alignment: railOpen ? .leading : .center, spacing: 3) {
                    if railOpen { railGroup("Providers") }
                    ForEach(apiProviders) { railTab($0) }
                    railSeparator("Subscriptions")
                    ForEach(cliProviders) { railTab($0) }
                }
            }
            railSeparator("Apps")
            appTab(.claude, "Claude Desktop", Chroma.claude, logo: "claude", live: model.claudeRunning && model.profileActive, count: model.claudeCatalogue.count)
            appTab(.codex, "Codex / ChatGPT", Chroma.codex, logo: "codex", live: model.codexRunning && model.codexProfileActive, count: model.codexCuratedRoutes.count)
            Button { withAnimation(.easeOut(duration: 0.2)) { railOpen.toggle() } } label: {
                Image(systemName: railOpen ? "chevron.left" : "chevron.right").font(.system(size: 10, weight: .semibold)).foregroundStyle(.tertiary)
                    .frame(width: 24, height: 20)
            }.buttonStyle(.plain).padding(.top, 4)
        }
        .padding(.horizontal, railOpen ? 8 : 0).padding(.top, 4).padding(.bottom, 8)
        .animation(.easeOut(duration: 0.2), value: railOpen)
    }

    private func railGroup(_ title: String) -> some View {
        Text(title.uppercased()).font(.system(size: 9, weight: .semibold)).tracking(1).foregroundStyle(.tertiary).padding(.leading, 8).padding(.vertical, 3)
    }

    @ViewBuilder private func railSeparator(_ group: String) -> some View {
        if railOpen { railGroup(group).padding(.top, 6) }
        else { Rectangle().fill(Semantic.hairline).frame(width: 16, height: 1).padding(.vertical, 5) }
    }

    private func railTab(_ provider: ProviderDefinition) -> some View {
        let state = model.shellProviderState(provider.id)
        let active = tab == .provider(provider.id)
        return Button { tab = .provider(provider.id) } label: {
            HStack(spacing: 8) {
                ZStack(alignment: .bottomTrailing) {
                    ProviderMark(presentation: provider.presentation, size: 24).opacity(state.on ? 1 : 0.62)
                    Circle().fill(state.color).frame(width: 6, height: 6).overlay(Circle().stroke(Semantic.surface, lineWidth: 1)).offset(x: 1, y: 1)
                }.frame(width: 24, height: 24)
                if railOpen {
                    Text(provider.presentation.displayProvider).font(.system(size: 11.5, weight: .medium)).lineLimit(1).foregroundStyle(active ? .primary : .secondary)
                    Spacer(minLength: 0)
                    let count = model.providerModels(provider.id).count
                    if count > 0 { Text("\(count)").font(.system(size: 10, design: .monospaced)).foregroundStyle(.tertiary) }
                }
            }
            .padding(.horizontal, railOpen ? 6 : 0).frame(width: railOpen ? nil : 28, height: 28)
            .background { if active { railPill(provider.presentation.color) } }
        }.buttonStyle(.plain).help(provider.name)
    }

    private func appTab(_ which: ShellTab, _ title: String, _ accent: Color, logo: String, live: Bool, count: Int) -> some View {
        let active = tab == which
        let presentation = model.providerDefinitions.first { $0.id == logo }?.presentation
        return Button { tab = which } label: {
            HStack(spacing: 8) {
                ZStack(alignment: .topTrailing) {
                    Group {
                        if let presentation { ProviderMark(presentation: presentation, size: 20) }
                        else { Text(logo == "claude" ? "C" : "⌘").font(.system(size: 12, weight: .bold)).foregroundStyle(accent) }
                    }.frame(width: 30, height: 30)
                    if live { Circle().fill(Chroma.ok).frame(width: 7, height: 7).overlay(Circle().stroke(Semantic.surface, lineWidth: 1)).offset(x: 2, y: -2) }
                }
                if railOpen {
                    Text(title).font(.system(size: 11.5, weight: .medium)).lineLimit(1).foregroundStyle(active ? .primary : .secondary)
                    Spacer(minLength: 0)
                    Text("\(count)").font(.system(size: 10, design: .monospaced)).foregroundStyle(.tertiary)
                }
            }
            .padding(.horizontal, railOpen ? 4 : 0).frame(width: railOpen ? nil : 30, height: 30)
            .background(RoundedRectangle(cornerRadius: 8).fill(accent.opacity(active ? 0.22 : 0.10)))
            .overlay(RoundedRectangle(cornerRadius: 8).stroke(accent.opacity(active ? 0.55 : 0.25), lineWidth: 1))
        }.buttonStyle(.plain).help(title)
    }

    private func railPill(_ color: Color) -> some View {
        RoundedRectangle(cornerRadius: 8).fill(color.opacity(0.17))
            .overlay(RoundedRectangle(cornerRadius: 8).stroke(color.opacity(0.3), lineWidth: 1))
            .matchedGeometryEffect(id: "rail-pill", in: rail)
    }

    // MARK: main pane

    @ViewBuilder private var mainPane: some View {
        VStack(spacing: 0) {
            ScrollView {
                Group {
                    switch tab {
                    case .provider(let id):
                        if let provider = model.providerDefinitions.first(where: { $0.id == id }) {
                            if subscriptionProviders.contains(id) { SubscriptionPane(model: model, provider: provider, focused: $focusedRoute, search: $search) }
                            else { ProviderShellPane(model: model, provider: provider, focused: $focusedRoute, search: $search) }
                        } else { Text("Choose a provider").foregroundStyle(.secondary).padding() }
                    case .claude: ClaudeShellPane(model: model, jump: jump)
                    case .codex: CodexShellPane(model: model, jump: jump)
                    case .settings: EmptyView()
                    }
                }.padding(.horizontal, 10).padding(.vertical, 6).frame(maxWidth: .infinity, alignment: .topLeading)
            }
            if !model.notice.isEmpty { noticeBar }
        }
    }

    private func jump(_ route: String) {
        let provider = String(route.split(separator: "/", maxSplits: 1).first ?? "")
        tab = .provider(provider); DispatchQueue.main.async { focusedRoute = route }
    }

    private var noticeBar: some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: model.noticeIsError ? "exclamationmark.circle" : (model.noticeIsWarning ? "exclamationmark.triangle" : "checkmark.circle"))
                .foregroundStyle(model.noticeIsError ? Chroma.warn : (model.noticeIsWarning ? Color.yellow : Chroma.ok))
            Text(model.notice).font(.system(size: 11)).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
            Button { model.notice = "" } label: { Image(systemName: "xmark").font(.system(size: 9)) }.buttonStyle(.plain).foregroundStyle(.secondary)
        }.padding(10).background(Semantic.raisedSurface)
    }

    // MARK: side pane

    @ViewBuilder private var sidePane: some View {
        VStack(alignment: .leading, spacing: 8) {
            switch tab {
            case .provider(let id):
                if let provider = model.providerDefinitions.first(where: { $0.id == id }) {
                    ProviderSide(model: model, provider: provider, focused: $focusedRoute)
                }
            case .claude: AppSide(model: model, isClaude: true)
            case .codex: AppSide(model: model, isClaude: false)
            case .settings: EmptyView()
            }
        }.padding(.horizontal, 10).padding(.vertical, 6).frame(maxHeight: .infinity, alignment: .top)
    }

    // MARK: audit

    private var auditSheet: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack { Text("Audit").font(.system(size: 15, weight: .semibold)); Text("this gateway run").foregroundStyle(.secondary); Spacer(); Button("Close") { showAudit = false } }
            HStack(spacing: 8) {
                auditTile("\(model.completed)", "completed"); auditTile("\(model.activeRequests)", "in flight"); auditTile("\(model.failures)", "failed"); auditTile((model.inputTokens + model.outputTokens).formatted(), "tokens")
            }
            ScrollView {
                VStack(spacing: 2) {
                    ForEach(model.activity) { entry in
                        HStack(spacing: 8) {
                            Text(String(entry.time.dropFirst(11).prefix(8))).font(.system(size: 10, design: .monospaced)).foregroundStyle(.tertiary)
                            Text(entry.event).frame(width: 90, alignment: .leading)
                            Text(model.modelLabel(entry.model)).lineLimit(1).frame(maxWidth: .infinity, alignment: .leading)
                            Text(entry.status.map(String.init) ?? "").font(.system(size: 10, design: .monospaced)).foregroundStyle(entry.event == "completed" ? Chroma.ok : Chroma.warn)
                        }.font(.system(size: 11)).padding(.vertical, 2)
                    }
                    if model.activity.isEmpty { Text("No requests yet").foregroundStyle(.secondary).padding() }
                }
            }.frame(height: 220)
            HStack { Button("Open data folder") { NSWorkspace.shared.open(model.root) }; Spacer() }
        }.padding(16).frame(width: 560)
    }

    private func auditTile(_ value: String, _ label: String) -> some View {
        VStack(alignment: .leading, spacing: 2) { Text(value).font(.system(size: 16, weight: .semibold, design: .rounded)); Text(label).font(.system(size: 10)).foregroundStyle(.secondary) }
            .padding(8).frame(maxWidth: .infinity, alignment: .leading).background(Semantic.raisedSurface, in: RoundedRectangle(cornerRadius: 8))
    }
}

// MARK: - Shared bits

private struct PaneHeader: View {
    var presentation: ProviderPresentation?
    var title: String
    var subtitle: String
    var accent: Color
    var state: (text: String, color: Color)?
    var body: some View {
        HStack(spacing: 8) {
            if let presentation { ProviderMark(presentation: presentation, size: 22) }
            Text(title).font(.system(size: 13.5, weight: .bold)).foregroundStyle(accent)
            Text(subtitle).font(.system(size: 11)).foregroundStyle(.secondary).lineLimit(1)
            Spacer()
            if let state { Pill(text: state.text, color: state.color) }
        }.padding(.vertical, 4)
    }
}

private struct Pill: View {
    var text: String; var color: Color
    var body: some View {
        Text(text).font(.system(size: 9.5, weight: .semibold)).foregroundStyle(color)
            .padding(.horizontal, 7).padding(.vertical, 2).background(color.opacity(0.18), in: Capsule())
    }
}

private struct SectionLabel: View {
    var text: String; var trailing: String = ""
    var body: some View {
        HStack { Text(text.uppercased()).tracking(0.6); Spacer(); Text(trailing) }
            .font(.system(size: 9.5, weight: .semibold)).foregroundStyle(.tertiary).padding(.top, 8).padding(.bottom, 4)
    }
}

private struct ModeCard: View {
    var title: String; var code: String; var selected: Bool; var accent: Color; var action: () -> Void
    var body: some View {
        Button(action: action) {
            VStack(alignment: .leading, spacing: 2) {
                HStack { Text(title).font(.system(size: 11.5, weight: .semibold)); Spacer(); if selected { Image(systemName: "checkmark").font(.system(size: 9, weight: .bold)).foregroundStyle(accent) } }
                Text(code).font(.system(size: 9.5, design: .monospaced)).foregroundStyle(.tertiary)
            }.padding(8).frame(maxWidth: .infinity, alignment: .leading)
            .background(RoundedRectangle(cornerRadius: 9).fill(selected ? accent.opacity(0.14) : Semantic.raisedSurface))
            .overlay(RoundedRectangle(cornerRadius: 9).stroke(selected ? accent.opacity(0.55) : Semantic.hairline, lineWidth: 1))
        }.buttonStyle(.plain)
    }
}

private struct OptionRow: View {
    var title: String; @Binding var isOn: Bool; var disabled = false; var accent: Color = Chroma.one
    var body: some View {
        Toggle(isOn: $isOn) { Text(title).foregroundStyle(.secondary).frame(maxWidth: .infinity, alignment: .leading) }
            .toggleStyle(.switch).controlSize(.mini).tint(accent).disabled(disabled)
    }
}

/// One model row: name, tier tag, CL/CX chips, fast bolt, check.
private struct ModelRow: View {
    @ObservedObject var model: BridgeModel
    var entry: ModelEntry
    var accent: Color
    var focused: Bool
    var onFocus: () -> Void
    var body: some View {
        let claude = model.claudeEntry(entry.id)
        let inCodex = model.codexCuratedRoutes.contains(entry.id) && model.settings.codex_catalogue != nil
        let listed = claude != nil || inCodex
        Button(action: onFocus) {
            HStack(spacing: 7) {
                Text(model.modelLabel(entry.id)).font(.system(size: 12.5, weight: listed ? .medium : .regular)).foregroundStyle(listed ? .primary : .secondary).lineLimit(1)
                Spacer(minLength: 4)
                if let claude {
                    Text((claudeTiers.first { $0.id == claude.tier }?.label ?? claude.tier) + (model.isClaudeTierDefault(claude) ? " ★" : ""))
                        .font(.system(size: 9.5, weight: .semibold)).foregroundStyle(Chroma.tiers[claude.tier] ?? .secondary)
                }
                chip("CL", claude != nil, Chroma.claude) { toggleClaude() }
                chip("CX", inCodex, Chroma.codex) { model.codexMaterialise(); model.codexSetListed(entry.id, !inCodex) }
                Image(systemName: "bolt.fill").font(.system(size: 9)).foregroundStyle(accent.opacity(entry.fast_mode == true ? 0.85 : 0)).frame(width: 10)
                Image(systemName: "checkmark").font(.system(size: 10, weight: .semibold)).foregroundStyle(accent.opacity(listed ? 1 : 0)).frame(width: 12)
            }
            .padding(.horizontal, 8).padding(.vertical, 5)
            .background(RoundedRectangle(cornerRadius: 6).fill(focused ? accent.opacity(0.18) : .clear))
        }.buttonStyle(.plain)
    }
    private func toggleClaude() {
        if model.claudeEntry(entry.id) != nil { model.removeClaudeRoute(entry.id) }
        else { if model.settings.claude_catalogue == nil { model.settings.claude_catalogue = model.claudeCatalogue }; model.addClaudeRoute(entry.id) }
    }
    private func chip(_ text: String, _ on: Bool, _ color: Color, _ action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text(text).font(.system(size: 8.5, weight: .bold)).tracking(0.4)
                .foregroundStyle(on ? color : Color.secondary.opacity(0.7))
                .padding(.horizontal, 4).padding(.vertical, 1)
                .background(RoundedRectangle(cornerRadius: 4).fill(on ? color.opacity(0.22) : Semantic.selection))
        }.buttonStyle(.plain)
    }
}

// MARK: - Provider pane

private struct ProviderShellPane: View {
    @ObservedObject var model: BridgeModel
    var provider: ProviderDefinition
    @Binding var focused: String?
    @Binding var search: String
    var connection: ProviderConnection? { model.settings.providers[provider.id] }
    var models: [ModelEntry] {
        let all = model.providerModels(provider.id)
        let query = search.trimmingCharacters(in: .whitespaces)
        return query.isEmpty ? all : all.filter { (model.modelLabel($0.id) + " " + $0.id).localizedCaseInsensitiveContains(query) }
    }
    var listedCount: Int { model.providerModels(provider.id).filter { model.claudeEntry($0.id) != nil || (model.settings.codex_catalogue?.contains($0.id) ?? false) }.count }

    var body: some View {
        let state = model.shellProviderState(provider.id)
        VStack(alignment: .leading, spacing: 4) {
            PaneHeader(presentation: provider.presentation, title: provider.presentation.displayProvider, subtitle: credentialSubtitle, accent: provider.presentation.color, state: (state.text, state.color))
            SectionLabel(text: provider.id == "ollama" ? "Daemon" : "Credential")
            credentialBlock
            if let issue = model.providerRefreshIssues[provider.id] {
                Text(issue).font(.system(size: 10.5)).foregroundStyle(Chroma.warn).fixedSize(horizontal: false, vertical: true)
            }
            SectionLabel(text: "Models", trailing: model.providerModels(provider.id).isEmpty ? "" : "\(listedCount) / \(model.providerModels(provider.id).count) listed")
            if model.providerModels(provider.id).count > 6 {
                TextField("Find a model", text: $search).textFieldStyle(.roundedBorder).controlSize(.small)
            }
            if model.providerModels(provider.id).isEmpty {
                Text(state.on ? "No models reported yet. Refresh to retry." : "Save a key to list models.").foregroundStyle(.secondary).frame(maxWidth: .infinity).padding(.vertical, 18)
            } else {
                LazyVStack(spacing: 1) {
                    ForEach(models) { entry in
                        ModelRow(model: model, entry: entry, accent: provider.presentation.color, focused: focused == entry.id) { focused = entry.id }
                    }
                }
            }
        }
    }

    private var credentialSubtitle: String {
        switch connection?.credential_mode {
        case "vibe": return "API key · Vibe"
        case "cli": return "Installed CLI login"
        case "environment": return "Environment variable"
        default: return provider.id == "ollama" ? "Local daemon" : "API key"
        }
    }

    @ViewBuilder private var credentialBlock: some View {
        VStack(alignment: .leading, spacing: 6) {
            if provider.id == "ollama" {
                HStack {
                    TextField("Daemon address", text: field(\.base_url)).textFieldStyle(.roundedBorder).controlSize(.small)
                    Button("Reconnect") { Task { await model.reconnect() } }.controlSize(.small).disabled(model.busy)
                }
                HStack { Text("Unload a finished model after").foregroundStyle(.secondary); Spacer()
                    Picker("", selection: model.ollamaIdleUnload) { ForEach([0, 30, 60, 90, 300, 900, -1], id: \.self) { Text(idleLabel($0)).tag($0) } }.labelsHidden().controlSize(.small).frame(width: 150) }
            } else {
                HStack(spacing: 8) {
                    Picker("", selection: field(\.credential_mode)) {
                        let modes = provider.credential_modes ?? ((provider.id == "mistral" ? ["vibe"] : []) + ["keychain", "environment"])
                        if modes.contains("vibe") { Text("Vibe saved key").tag("vibe") }
                        if modes.contains("keychain") { Text("Paste a key").tag("keychain") }
                        if modes.contains("environment") { Text("Environment").tag("environment") }
                        if modes.contains("cli") { Text("Installed CLI").tag("cli") }
                    }.pickerStyle(.segmented).labelsHidden().controlSize(.small).fixedSize()
                    if provider.regions.count > 1 {
                        Picker("", selection: Binding(get: { connection?.region ?? "" }, set: { region in
                            model.settings.providers[provider.id]?.region = region
                            model.settings.providers[provider.id]?.base_url = provider.regions[region] ?? ""
                        })) { ForEach(provider.regions.keys.sorted(), id: \.self) { Text(regionLabel($0)).tag($0) } }.labelsHidden().controlSize(.small).frame(width: 120)
                    }
                    Spacer()
                }
                switch connection?.credential_mode {
                case "keychain":
                    if keyAccountProviders.contains(provider.id) { KeyAccountsSection(model: model, provider: provider.id).controlSize(.small) }
                    HStack(spacing: 8) {
                        SecureField(keyPlaceholder, text: $model.secretDraft)
                            .textFieldStyle(.roundedBorder).controlSize(.small).font(.system(size: 11.5, design: .monospaced))
                        Button { Task { await model.saveKey() } } label: { Text("Save key").foregroundStyle(HubTheme.Control.ink) }
                            .buttonStyle(.borderedProminent).tint(HubTheme.Accent.brand).controlSize(.small).disabled(model.busy || model.secretDraft.isEmpty)
                    }
                case "vibe":
                    HStack { Text(model.vibeAlias.isEmpty ? "Start Vibe and sign in, then reconnect." : "Vibe model: " + model.vibeAlias).foregroundStyle(.secondary); Spacer(); Button("Open Vibe") { model.openVibe() }.controlSize(.small) }
                case "cli":
                    if cliAccountProviders.contains(provider.id) { CliAccountsSection(model: model, provider: provider.id).controlSize(.small) }
                    else { Text("Sign in through the \(provider.presentation.displayProvider) CLI itself, then save.").foregroundStyle(.secondary) }
                default:
                    Text("Reads \(provider.credential_env ?? "the provider key") from the app’s launch environment.").foregroundStyle(.secondary)
                }
            }
        }
    }

    private func field(_ key: WritableKeyPath<ProviderConnection, String>) -> Binding<String> {
        Binding(get: { model.settings.providers[provider.id]?[keyPath: key] ?? "" }, set: { model.settings.providers[provider.id]?[keyPath: key] = $0 })
    }
    /// Names the slot a pasted key lands in, and whether it already holds one.
    private var keyPlaceholder: String {
        let active = connection?.key_account
        let label = connection?.key_accounts?.first { $0.id == active }?.label
        let found = model.keyAccountStates[provider.id].map { states in states.first { $0.id == active }?.found ?? false }
            ?? (model.providerStates[provider.id]?.credential_found == true)
        let suffix = label.map { " for \($0)" } ?? ""
        return found ? "Key saved\(suffix) ••••••••  (paste to replace)" : "Paste your \(provider.presentation.displayProvider) API key\(suffix)"
    }
    private func idleLabel(_ seconds: Int) -> String {
        if seconds < 0 { return "Keep in memory" }; if seconds == 0 { return "At turn end" }
        return seconds < 60 ? "\(seconds) s" : "\(seconds / 60) min"
    }
    private func regionLabel(_ region: String) -> String { ["ams": "Amsterdam", "sgp": "Singapore", "cn": "China"][region] ?? region.capitalized }
}

/// Claude Code, Codex CLI, Antigravity, Devin: accounts and the routes the CLI advertises.
private struct SubscriptionPane: View {
    @ObservedObject var model: BridgeModel
    var provider: ProviderDefinition
    @Binding var focused: String?
    @Binding var search: String
    var body: some View {
        let state = model.shellProviderState(provider.id)
        VStack(alignment: .leading, spacing: 4) {
            PaneHeader(presentation: provider.presentation, title: provider.presentation.displayProvider, subtitle: provider.id == "devin" ? "Agent sessions" : "Subscription · CLI route", accent: provider.presentation.color, state: (state.text, state.color))
            SectionLabel(text: provider.id == "devin" ? "Credential" : "Accounts")
            if provider.id == "devin" {
                HStack(spacing: 8) {
                    SecureField("Devin API key (cog_ / pat_ / apk_)", text: $model.secretDraft).textFieldStyle(.roundedBorder).controlSize(.small)
                    Button("Save key") { Task { await model.saveKey() } }.controlSize(.small).disabled(model.busy || model.secretDraft.isEmpty)
                }
            } else {
                Picker("", selection: Binding(get: { model.settings.providers[provider.id]?.credential_mode ?? "cli" }, set: { model.settings.providers[provider.id]?.credential_mode = $0 })) {
                    Text("Installed CLI").tag("cli"); Text("Paste a key").tag("keychain")
                }.pickerStyle(.segmented).labelsHidden().controlSize(.small).fixedSize()
                if model.settings.providers[provider.id]?.credential_mode == "keychain" {
                    HStack(spacing: 8) {
                        SecureField("API key", text: $model.secretDraft).textFieldStyle(.roundedBorder).controlSize(.small)
                        Button("Save key") { Task { await model.saveKey() } }.controlSize(.small).disabled(model.busy || model.secretDraft.isEmpty)
                    }
                } else if cliAccountProviders.contains(provider.id) {
                    CliAccountsSection(model: model, provider: provider.id).controlSize(.small)
                } else {
                    Text("Sign in through the \(provider.presentation.displayProvider) CLI itself, then save.").foregroundStyle(.secondary)
                }
            }
            SectionLabel(text: "Routes", trailing: "\(model.providerModels(provider.id).count)")
            if model.providerModels(provider.id).isEmpty {
                Text(state.on ? "No routes reported. Refresh to retry." : "Sign in to list routes.").foregroundStyle(.secondary).frame(maxWidth: .infinity).padding(.vertical, 14)
            } else {
                LazyVStack(spacing: 1) {
                    ForEach(model.providerModels(provider.id)) { entry in
                        ModelRow(model: model, entry: entry, accent: provider.presentation.color, focused: focused == entry.id) { focused = entry.id }
                    }
                }
            }
        }
    }
}

// MARK: - Provider side column

private struct ProviderSide: View {
    @ObservedObject var model: BridgeModel
    var provider: ProviderDefinition
    @Binding var focused: String?
    var body: some View {
        let state = model.shellProviderState(provider.id)
        let models = model.providerModels(provider.id)
        VStack(alignment: .leading, spacing: 8) {
            SectionLabel(text: "Status")
            Text(models.isEmpty ? "—" : "\(models.count) models").font(.system(size: 18, weight: .semibold))
            Text(models.isEmpty ? state.text : "\(models.filter { model.claudeEntry($0.id) != nil || (model.settings.codex_catalogue?.contains($0.id) ?? false) }.count) listed").font(.system(size: 10.5)).foregroundStyle(.secondary)
            VStack(alignment: .leading, spacing: 4) {
                Button("Refresh") { Task { await model.discover() } }.disabled(model.busy || model.catalogueRefreshing || !state.on)
                Button("Account ↗") { if let url = URL(string: provider.setup_url) { NSWorkspace.shared.open(url) } }
                Button("Reconnect") { Task { await model.reconnect() } }.disabled(model.busy)
            }.controlSize(.small)
            if let route = focused, let entry = model.modelEntry(route), entry.provider_id == provider.id {
                Divider().padding(.vertical, 2)
                selected(entry)
            } else {
                Spacer()
                Text(models.isEmpty ? "" : "Pick a model to set its Claude tier, which apps list it, and its subagent priority.").font(.system(size: 10.5)).foregroundStyle(.tertiary)
            }
        }
    }

    @ViewBuilder private func selected(_ entry: ModelEntry) -> some View {
        let claude = model.claudeEntry(entry.id)
        let inCodex = model.settings.codex_catalogue?.contains(entry.id) ?? false
        VStack(alignment: .leading, spacing: 6) {
            SectionLabel(text: "Selected")
            Text(model.modelLabel(entry.id)).font(.system(size: 12, weight: .semibold)).lineLimit(2)
            Text(entry.id).font(.system(size: 9.5, design: .monospaced)).foregroundStyle(.tertiary).lineLimit(1).truncationMode(.middle)
            Text(model.contextLabel(entry) + " context" + (entry.fast_mode == true ? " · Fast" : "")).font(.system(size: 10.5)).foregroundStyle(.secondary)
            SectionLabel(text: "Claude tier")
            tierPills(entry, claude)
            VStack(spacing: 6) {
                OptionRow(title: "Tier default", isOn: Binding(get: { claude.map(model.isClaudeTierDefault) ?? false }, set: { if $0 { model.makeClaudeTierDefault(entry.id) } }), disabled: claude == nil, accent: provider.presentation.color)
                OptionRow(title: "Claude", isOn: Binding(get: { claude != nil }, set: { on in
                    if on { if model.settings.claude_catalogue == nil { model.settings.claude_catalogue = model.claudeCatalogue }; model.addClaudeRoute(entry.id) } else { model.removeClaudeRoute(entry.id) } }), accent: Chroma.claude)
                OptionRow(title: "Codex", isOn: Binding(get: { inCodex }, set: { model.codexMaterialise(); model.codexSetListed(entry.id, $0) }), accent: Chroma.codex)
                HStack { Text("Subagent priority").foregroundStyle(.secondary); Spacer()
                    Stepper("", value: Binding(get: { model.settings.codex_subagent_rank?[entry.id] ?? 0 }, set: { model.codexSetRank(entry.id, $0 == 0 ? nil : $0) }), in: 0...5).labelsHidden().controlSize(.mini)
                    Text(model.settings.codex_subagent_rank?[entry.id].map(String.init) ?? "auto").font(.system(size: 10.5, design: .monospaced)).frame(width: 30, alignment: .trailing) }
            }.padding(.top, 4)
        }
    }

    private func tierPills(_ entry: ModelEntry, _ claude: ClaudeCatalogueEntry?) -> some View {
        let current = claude?.tier
        let accent = provider.presentation.color
        return FlowRow(spacing: 4) {
            pill("Off", current == nil, accent) { model.removeClaudeRoute(entry.id) }
            ForEach(tierOrder, id: \.self) { tier in
                pill(claudeTiers.first { $0.id == tier }?.label ?? tier, current == tier, accent) {
                    if model.settings.claude_catalogue == nil { model.settings.claude_catalogue = model.claudeCatalogue }
                    if model.claudeEntry(entry.id) == nil { model.addClaudeRoute(entry.id, tier: tier) } else { model.claudeTier(for: entry.id).wrappedValue = tier }
                }
            }
        }
    }
    private func pill(_ text: String, _ on: Bool, _ accent: Color, _ action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text(text).font(.system(size: 10, weight: .semibold)).foregroundStyle(on ? .primary : .secondary)
                .padding(.horizontal, 7).padding(.vertical, 2)
                .background(Capsule().fill(on ? accent.opacity(0.22) : Semantic.raisedSurface))
                .overlay(Capsule().stroke(on ? accent.opacity(0.55) : Semantic.hairline, lineWidth: 1))
        }.buttonStyle(.plain)
    }
}

/// Wrapping row of small controls.
private struct FlowRow<Content: View>: View {
    var spacing: CGFloat = 4
    @ViewBuilder var content: Content
    var body: some View {
        // Five pills fit on one line at the side column's width; a plain HStack keeps layout simple.
        HStack(spacing: spacing) { content }.frame(maxWidth: .infinity, alignment: .leading)
    }
}

// MARK: - App panes

private struct ClaudeShellPane: View {
    @ObservedObject var model: BridgeModel
    var jump: (String) -> Void
    var body: some View {
        let presentation = model.providerDefinitions.first { $0.id == "claude" }?.presentation
        VStack(alignment: .leading, spacing: 4) {
            PaneHeader(presentation: presentation, title: "Claude Desktop", subtitle: model.claudeInstalled ? "profile “Provider Hub”" : "not installed", accent: Chroma.claude,
                       state: (model.profileActive && model.claudeRunning ? "Running" : "Closed", model.profileActive && model.claudeRunning ? Chroma.ok : Color.secondary))
            SectionLabel(text: "Catalogue", trailing: "\(model.claudeCatalogue.count) listed")
            HStack(spacing: 6) {
                ModeCard(title: "Everything", code: "curated(auto)", selected: model.claudeMode == "all", accent: Chroma.claude) { model.setClaudeMode("all") }
                ModeCard(title: "Curate", code: "curated", selected: model.claudeMode == "curate", accent: Chroma.claude) { model.setClaudeMode("curate") }
                ModeCard(title: "Five slots", code: "mappings", selected: model.claudeMode == "slots", accent: Chroma.claude) { model.setClaudeMode("slots") }
            }
            if model.claudeMode == "slots" {
                SectionLabel(text: "Slots")
                ForEach(slots, id: \.id) { slot in
                    HStack { Text(slot.label).font(.system(size: 11.5, weight: .semibold)).frame(width: 70, alignment: .leading)
                        Picker("", selection: Binding(get: { model.settings.mappings[slot.id] ?? "" }, set: { model.settings.mappings[slot.id] = $0 })) {
                            ForEach(model.routeOptions, id: \.self) { route in Text(model.modelLabel(route)).tag(route) }
                        }.labelsHidden().controlSize(.small) }
                }
            } else {
                SectionLabel(text: "Tier defaults")
                ForEach(claudeTiers, id: \.id) { tier in
                    HStack(spacing: 8) {
                        Text(tier.label).font(.system(size: 11.5, weight: .semibold)).frame(width: 52, alignment: .leading)
                        if let entry = model.claudeCatalogue.first(where: { $0.tier == tier.id && model.isClaudeTierDefault($0) }) {
                            if let presentation = model.modelEntry(entry.route)?.presentation { ProviderMark(presentation: presentation, size: 16) }
                            Text(model.modelLabel(entry.route)).lineLimit(1)
                            Spacer()
                            Button("Change") { jump(entry.route) }.buttonStyle(.borderless).font(.system(size: 10.5)).foregroundStyle(.secondary)
                        } else {
                            Text("none · nearest tier stands in").foregroundStyle(.tertiary); Spacer()
                        }
                    }.padding(.horizontal, 8).padding(.vertical, 4).background(Semantic.raisedSurface, in: RoundedRectangle(cornerRadius: 6))
                }
            }
        }
    }
}

private struct CodexShellPane: View {
    @ObservedObject var model: BridgeModel
    var jump: (String) -> Void
    var body: some View {
        let presentation = model.providerDefinitions.first { $0.id == "codex" }?.presentation
        let running = model.codexRunning && model.codexProfileActive
        VStack(alignment: .leading, spacing: 4) {
            PaneHeader(presentation: presentation, title: "Codex / ChatGPT", subtitle: model.codexAppPath == nil ? "not installed" : "desktop app", accent: Chroma.codex,
                       state: (running ? "Running" : "Closed", running ? Chroma.ok : Color.secondary))
            SectionLabel(text: "Catalogue", trailing: "\(model.codexCuratedRoutes.count) listed")
            HStack(spacing: 6) {
                ModeCard(title: "All compatible", code: "all_compatible", selected: model.codexMode == "all", accent: Chroma.codex) { model.setCodexMode("all") }
                ModeCard(title: "Custom", code: "custom", selected: model.codexMode == "custom", accent: Chroma.codex) { model.setCodexMode("custom") }
            }
            SectionLabel(text: "Default model")
            Picker("", selection: Binding(get: { model.settings.codex_model ?? "" }, set: { model.codexMaterialise(); model.settings.codex_model = $0.isEmpty ? nil : $0 })) {
                Text("Choose a model…").tag("")
                ForEach(model.codexCuratedRoutes, id: \.self) { route in Text(model.modelLabel(route)).tag(route) }
            }.labelsHidden().controlSize(.small).frame(maxWidth: 260, alignment: .leading)
            SectionLabel(text: "Subagent priority")
            ForEach(1...5, id: \.self) { rank in
                let holders = (model.settings.codex_subagent_rank ?? [:]).filter { $0.value == rank }.map(\.key).sorted()
                HStack(spacing: 8) {
                    Text("\(rank)").font(.system(size: 10, weight: .semibold, design: .monospaced)).foregroundStyle(.tertiary).frame(width: 14)
                    Picker("", selection: Binding(get: { holders.first ?? "" }, set: { route in
                        model.codexMaterialise()
                        var ranks = (model.settings.codex_subagent_rank ?? [:]).filter { $0.value != rank }
                        if !route.isEmpty { ranks[route] = rank }
                        model.settings.codex_subagent_rank = ranks.isEmpty ? nil : ranks })) {
                        Text("Automatic").tag("")
                        ForEach(model.codexCuratedRoutes, id: \.self) { route in Text(model.modelLabel(route)).tag(route) }
                    }.labelsHidden().controlSize(.small)
                    if holders.count > 1 { Text("+\(holders.count - 1)").foregroundStyle(Chroma.warn) }
                }
            }
        }
    }
}

private struct AppSide: View {
    @ObservedObject var model: BridgeModel
    var isClaude: Bool
    var body: some View {
        let accent = isClaude ? Chroma.claude : Chroma.codex
        let count = isClaude ? model.claudeCatalogue.count : model.codexCuratedRoutes.count
        let running = isClaude ? (model.claudeRunning && model.profileActive) : (model.codexRunning && model.codexProfileActive)
        VStack(alignment: .leading, spacing: 8) {
            SectionLabel(text: isClaude ? "Claude" : "Codex")
            Text("\(count)").font(.system(size: 18, weight: .semibold))
            Text("in picker").font(.system(size: 10.5)).foregroundStyle(.secondary)
            Button { Task { if isClaude { await model.launchClaude() } else { await model.launchCodex() } } } label: {
                Text(running ? (isClaude ? "Show Claude" : "Show Codex") : (isClaude ? "Launch Claude" : "Launch Codex")).foregroundStyle(HubTheme.Control.ink).frame(maxWidth: .infinity)
            }.buttonStyle(.borderedProminent).tint(accent).controlSize(.regular)
                .disabled(model.busy || (isClaude ? !model.claudeInstalled : (model.codexAppPath == nil || model.settings.codex_model == nil)))
            Button("Restore") { Task { if isClaude { await model.restore() } else { await model.restoreCodex() } } }.controlSize(.small)
                .disabled(model.busy || (isClaude ? (!model.recoveryNeeded || model.claudeRunning) : (!model.codexRecoveryNeeded || model.codexRunning)))
            if isClaude && model.claudeRunning && !model.profileActive {
                Text("Claude is open with another profile. Quit it first.").font(.system(size: 10.5)).foregroundStyle(Chroma.warn)
            }
            if !isClaude && model.codexRunning && !model.codexProfileActive {
                Text("The app is open. Launching asks before restarting it.").font(.system(size: 10.5)).foregroundStyle(Chroma.warn)
            }
            Spacer()
        }
    }
}

/// Sun / moon / screen: Light, Dark or System (the default). Writes the same
/// preference as the status menu's Appearance submenu and applies it at once.
private struct AppearanceToggle: View {
    @AppStorage(HubTheme.Appearance.defaultsKey) private var stored = HubTheme.Appearance.Mode.system.rawValue
    private var mode: Binding<HubTheme.Appearance.Mode> {
        Binding(get: { HubTheme.Appearance.Mode(rawValue: stored) ?? .system },
                set: { stored = $0.rawValue; HubTheme.Appearance.apply() })
    }
    var body: some View {
        Picker("", selection: mode) {
            Image(systemName: "sun.max").tag(HubTheme.Appearance.Mode.light).help("Light")
            Image(systemName: "moon").tag(HubTheme.Appearance.Mode.dark).help("Dark")
            Image(systemName: "display").tag(HubTheme.Appearance.Mode.system).help("System")
        }.pickerStyle(.segmented).labelsHidden().controlSize(.small).fixedSize()
    }
}

// MARK: - Settings page (the cog)

/// Every desktop-app preference and the gateway settings, on their own page
/// so the app tabs stay about the catalogue and launching.
private struct SettingsPage: View {
    @ObservedObject var model: BridgeModel
    var body: some View {
        ScrollView {
            HStack(alignment: .top, spacing: 18) {
                VStack(alignment: .leading, spacing: 6) {
                    PaneHeader(presentation: model.providerDefinitions.first { $0.id == "claude" }?.presentation, title: "Claude Desktop", subtitle: "profile preferences", accent: Chroma.claude, state: nil)
                    SectionLabel(text: "Claude Code")
                    option("Teach Claude Code the catalogue ids", "A modelPicker row per catalogue model with behavesAs on its tier, written while the profile is active.", $model.settings.claude_code_settings, Chroma.claude)
                    option("Enable Ultracode workflows", "Adds enableWorkflows while the profile is active; Fable, Opus and Sonnet tiers only.", $model.settings.claude_workflows, Chroma.claude)
                    SectionLabel(text: "Permissions")
                    option("Auto mode", "Claude chooses its approval reviewer internally.", $model.settings.auto_mode, Chroma.claude)
                    SectionLabel(text: "Features")
                    option("Dictation", nil, $model.settings.claude_features.dictation, Chroma.claude)
                    option("Built-in browser", nil, $model.settings.claude_features.builtin_browser, Chroma.claude)
                    option("Claude in Chrome", nil, $model.settings.claude_features.claude_in_chrome, Chroma.claude)
                    option("Scheduled tasks", nil, $model.settings.claude_features.scheduled_tasks, Chroma.claude)
                    option("Cowork tab", "Features apply at the next Claude launch.", $model.settings.claude_features.cowork_tab, Chroma.claude)
                }.frame(maxWidth: .infinity, alignment: .topLeading)
                VStack(alignment: .leading, spacing: 6) {
                    PaneHeader(presentation: model.providerDefinitions.first { $0.id == "codex" }?.presentation, title: "Codex / ChatGPT", subtitle: "app preferences", accent: Chroma.codex, state: nil)
                    SectionLabel(text: "Account and appearance")
                    option("Show your ChatGPT account", "Keeps the sign-in and native account styling; the gateway credential is written for the session.", $model.settings.codex_chatgpt_account, Chroma.codex)
                    option("Provider accent colours", "Launches through the DevTools-pipe helper that tints the power slider per model. Unsupported by OpenAI.", $model.settings.codex_accent_slider, Chroma.codex)
                    option("Recent-thread quick composer", "Floating prompt popover for recent local chats; same helper.", $model.settings.codex_quick_composer, Chroma.codex)
                    option("Hide the ChatGPT usage banner", model.settings.codex_accent_slider ? nil : "Needs provider accent colours.", $model.settings.codex_hide_usage_banner, Chroma.codex, disabled: !model.settings.codex_accent_slider)
                    option("Keep sending when usage runs out", model.settings.codex_accent_slider ? nil : "Needs provider accent colours.", $model.settings.codex_unlock_composer, Chroma.codex, disabled: !model.settings.codex_accent_slider)
                    SectionLabel(text: "Tools and goals")
                    option("Patch-based file editing", "Supported edits appear in Review and Undo.", $model.settings.codex_apply_patch_all, Chroma.codex)
                    option("Allow goal token budgets", "Off removes token_budget from create_goal and update_goal.", $model.settings.codex_goal_budget, Chroma.codex)
                    SectionLabel(text: "Gateway")
                    option("Stop the gateway when both apps close", "The menu bar app stays available.", $model.settings.auto_stop, Chroma.one)
                    HStack { Text("Port").foregroundStyle(.secondary)
                        TextField("", value: $model.settings.port, format: .number.grouping(.never)).textFieldStyle(.roundedBorder).controlSize(.small).frame(width: 80)
                        Spacer() }
                    Text("Changing shared settings while a desktop app is live requires quitting both apps first.").font(.system(size: 10.5)).foregroundStyle(.tertiary).fixedSize(horizontal: false, vertical: true)
                    SectionLabel(text: "Hub")
                    HStack { Text("Appearance").foregroundStyle(.secondary); Spacer(); AppearanceToggle() }
                }.frame(maxWidth: .infinity, alignment: .topLeading)
            }.padding(14)
        }
    }
    private func option(_ title: String, _ detail: String?, _ binding: Binding<Bool>, _ accent: Color, disabled: Bool = false) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            OptionRow(title: title, isOn: binding, disabled: disabled, accent: accent)
            if let detail { Text(detail).font(.system(size: 10)).foregroundStyle(.tertiary).fixedSize(horizontal: false, vertical: true) }
        }.padding(.vertical, 2)
    }
}
