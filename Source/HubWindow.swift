import AppKit
import SwiftUI

// Provider Hub's one window. Providers (the Hub) and Code (Chat) are its two
// halves, chosen by a switch at the top of the sidebar. Both halves live in
// one view hierarchy and only one is shown, so switching never rebuilds a
// view: unsaved hub edits, chats, split panes and scroll positions all stay
// where they were. The title row of each half carries the gateway and the
// Claude and Codex launchers, so a launch is one click from either.

private typealias Semantic = HubTheme.Semantic

enum HubWindowTab: String, CaseIterable {
    case providers, code

    var title: String { self == .providers ? "Providers" : "Code" }
    var help: String { self == .providers ? "Providers, models and the desktop apps (⌘1)" : "Chat with a model in your folders (⌘2)" }
}

/// Which half shows. The choice persists, so the window reopens where it was
/// left; AppDelegate does the AppKit side of a switch through `onSelect`.
@MainActor
final class HubWindowTabs: ObservableObject {
    static let defaultsKey = "hubWindowTab"
    @Published private(set) var current: HubWindowTab
    var onSelect: ((HubWindowTab) -> Void)?
    private let defaults: UserDefaults

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        current = HubWindowTab(rawValue: defaults.string(forKey: Self.defaultsKey) ?? "") ?? .providers
    }

    func select(_ tab: HubWindowTab) {
        guard tab != current else { return }
        current = tab
        defaults.set(tab.rawValue, forKey: Self.defaultsKey)
        onSelect?(tab)
    }
}

/// Present when a view is one half of the combined window: the switch's state,
/// the hub model behind the title-row controls, and how to bring the Providers
/// half forward when a hub action leaves a notice that only it shows.
struct HubWindowContext {
    let tabs: HubWindowTabs
    let hub: BridgeModel
    let attention: @MainActor () -> Void
}

private struct HubWindowContextKey: EnvironmentKey {
    static let defaultValue: HubWindowContext? = nil
}

extension EnvironmentValues {
    var hubWindow: HubWindowContext? {
        get { self[HubWindowContextKey.self] }
        set { self[HubWindowContextKey.self] = newValue }
    }
}

enum HubWindowMetrics {
    /// The title row both halves draw in the hidden title bar's band.
    static let titleHeight: CGFloat = 36
    /// The sidebar header under it, which holds the switch.
    static let switchBandHeight: CGFloat = 36
    /// Lines the switch up with the traffic lights above it.
    static let switchInset: CGFloat = 12
}

// MARK: - The switch

/// "Providers | Code" as the shell mockup's .seg control: a capsule track with
/// the chosen half on a raised thumb. Its width is fixed, not the sidebar's,
/// so it sits exactly the same in both halves and inside Chat's narrowest rail.
struct HubModeSwitch: View {
    @ObservedObject var tabs: HubWindowTabs
    static let width: CGFloat = 136
    static let height: CGFloat = 26

    var body: some View {
        HStack(spacing: 2) {
            ForEach(HubWindowTab.allCases, id: \.self) { segment($0) }
        }
        .padding(3)
        .frame(width: Self.width, height: Self.height)
        .background(Capsule().fill(Semantic.raisedSurface))
        .overlay(Capsule().strokeBorder(Semantic.hairline, lineWidth: 1))
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Window")
    }

    private func segment(_ tab: HubWindowTab) -> some View {
        let on = tabs.current == tab
        return Button { tabs.select(tab) } label: {
            Text(tab.title)
                .font(.system(size: 11, weight: on ? .semibold : .medium))
                .foregroundStyle(on ? Semantic.ink : Semantic.secondaryInk)
                .lineLimit(1).fixedSize()
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .background {
                    if on {
                        Capsule().fill(Semantic.selection)
                            .overlay(Capsule().strokeBorder(Semantic.composerRim, lineWidth: 1))
                    }
                }
                .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .help(tab.help)
        .accessibilityLabel(tab.title)
        .accessibilityAddTraits(on ? .isSelected : [])
    }
}

/// The sidebar header that holds the switch, directly under the title row in
/// both halves. Chat puts it at the top of its rail; the hub, whose rail is
/// too narrow for it, gives it the band's full width.
struct HubModeSwitchBand: View {
    let tabs: HubWindowTabs
    var body: some View {
        HStack(spacing: 0) {
            HubModeSwitch(tabs: tabs)
            Spacer(minLength: 0)
        }
        .padding(.leading, HubWindowMetrics.switchInset)
        .frame(height: HubWindowMetrics.switchBandHeight)
    }
}

// MARK: - The title row

/// The app's name row in the hidden title bar's band, clear of the traffic
/// lights. Each half adds its own controls after the spacer.
struct HubTitleRow<Trailing: View>: View {
    @ViewBuilder var trailing: Trailing
    var body: some View {
        HStack(spacing: 8) {
            Image(nsImage: NSApp.applicationIconImage).resizable()
                .frame(width: 18, height: 18).clipShape(RoundedRectangle(cornerRadius: 5))
                .accessibilityHidden(true)
            Text("Provider Hub").font(.system(size: 12, weight: .semibold))
            HubUpdatePill()
            Spacer()
            trailing
        }
        .padding(.leading, 78).padding(.trailing, 12).frame(height: HubWindowMetrics.titleHeight)
    }
}

extension BridgeModel {
    /// Run a hub action from somewhere its notice cannot show (the menu bar,
    /// the Code half); an error or warning calls `attention`, which brings
    /// the Providers half forward to show it.
    func perform(_ action: @escaping @MainActor () async -> Void, attention: (@MainActor () -> Void)?) {
        Task { @MainActor in
            await action()
            if !notice.isEmpty, noticeIsError || noticeIsWarning { attention?() }
        }
    }
}

/// The gateway's light and its Start/Stop. The Providers row also shows the
/// address; Code leaves it out.
struct HubGatewayControl: View {
    @ObservedObject var model: BridgeModel
    var showsAddress = false
    var attention: (@MainActor () -> Void)?

    var body: some View {
        HStack(spacing: 8) {
            Circle().fill(model.running ? Chroma.ok : Color.secondary.opacity(0.5)).frame(width: 7, height: 7)
                .shadow(color: model.running ? Chroma.ok.opacity(0.5) : .clear, radius: 3)
                .help(model.running ? "Gateway running" : "Gateway " + model.gatewayState.lowercased())
                .accessibilityHidden(true)
            if showsAddress {
                Text("127.0.0.1:\(String(model.savedSettings.port))").font(.system(size: 10.5, design: .monospaced)).foregroundStyle(.secondary)
            }
            Button(model.running ? "Stop" : "Start") { model.perform({ await model.toggleGateway() }, attention: attention) }
                .buttonStyle(.bordered).controlSize(.mini).disabled(model.busy)
                .help(model.running ? "Stop the gateway" : "Start the gateway")
                .accessibilityLabel(model.running ? "Stop gateway" : "Start gateway")
        }
    }
}

/// "Launch" plus the two app marks, each with its own light: green while
/// the app runs on the hub profile, amber when it runs on another one,
/// grey when closed. One click launches (or shows) the app with the
/// current catalogue, as the app tab's button does.
struct HubLaunchCluster: View {
    @ObservedObject var model: BridgeModel
    var attention: (@MainActor () -> Void)?

    var body: some View {
        HStack(spacing: 4) {
            Text("Launch").font(.system(size: 10.5)).foregroundStyle(.secondary).padding(.trailing, 2)
            mark(claude: true)
            mark(claude: false)
        }
    }

    private func mark(claude isClaude: Bool) -> some View {
        let presentation = model.providerDefinitions.first { $0.id == (isClaude ? "claude" : "codex") }?.presentation
        let running = isClaude ? model.claudeRunning : model.codexRunning
        let live = running && (isClaude ? model.profileActive : model.codexProfileActive)
        let led: Color = live ? Chroma.ok : (running ? Chroma.warn : Color.secondary.opacity(0.5))
        let missing = isClaude ? (model.claudeInstalled ? nil : "Claude Desktop is not installed")
            : (model.codexAppPath == nil ? "Install the Codex / ChatGPT app first"
               : (model.settings.codex_model == nil ? "Choose a model for Codex / ChatGPT first" : nil))
        let name = isClaude ? "Claude" : "Codex"
        let action = live ? "Show " + name : "Launch " + name
        let detail = live ? " · running on the hub profile"
            : (running ? (isClaude ? " · open with another profile; quit it first" : " · open with another profile; asks before restarting it") : "")
        return Button { model.perform({ if isClaude { await model.launchClaude() } else { await model.launchCodex() } }, attention: attention) } label: {
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
            .opacity(model.busy || missing != nil ? 0.45 : 1)
        }
        .buttonStyle(.plain).disabled(model.busy || missing != nil)
        .help(missing ?? action + detail)
        .accessibilityLabel(action)
    }
}

/// What Code's title row adds: the gateway without its address, the
/// launchers, and the hub's busy spinner, since a launch can take a while.
struct HubQuickControls: View {
    @ObservedObject var model: BridgeModel
    var attention: (@MainActor () -> Void)?

    var body: some View {
        HStack(spacing: 8) {
            HubGatewayControl(model: model, attention: attention)
            HubLaunchCluster(model: model, attention: attention).padding(.leading, 10)
            if model.busy { ProgressView().controlSize(.mini) }
        }
    }
}

// MARK: - Classic pages

/// The classic pages as the Providers half. The window's title bar is drawn
/// by the content in both halves now, so they sit under the same title row
/// and switch as Code, on their own dark surface.
struct ClassicHubFrame: View {
    let model: BridgeModel
    let tabs: HubWindowTabs

    var body: some View {
        VStack(spacing: 0) {
            HubTitleRow { HubQuickControls(model: model) }
            HubModeSwitchBand(tabs: tabs)
            BridgeWindow(model: model)
        }
        .background(HubTheme.Surface.window)
        .ignoresSafeArea()
    }
}

// MARK: - AppKit

/// The hidden half keeps its views but never the keyboard. AppKit already
/// skips hidden views for clicks and key equivalents, but it will still make
/// one first responder, and a composer created while Code is hidden (a chat
/// opened from the companion, say) asks for focus as it appears.
final class HubWindow: NSWindow {
    override func makeFirstResponder(_ responder: NSResponder?) -> Bool {
        if let view = responder as? NSView, view.isHiddenOrHasHiddenAncestor { return false }
        return super.makeFirstResponder(responder)
    }
}

/// Holds one hosting view per half and shows one. Hiding, rather than removing
/// a half, keeps every SwiftUI state, task and scroll observer running exactly
/// as it was; the shown half gets back the focus it had when it was left.
@MainActor
final class HubWindowContent: NSView {
    private var hosts: [HubWindowTab: NSView] = [:]
    private var focus: [HubWindowTab: Remembered] = [:]
    private(set) var shown: HubWindowTab?

    private struct Remembered { weak var view: NSView? }

    func has(_ tab: HubWindowTab) -> Bool { hosts[tab] != nil }

    /// Install a half, replacing any earlier one (a design change rebuilds the
    /// hub's views; the model and its unsaved edits are untouched).
    func install<Root: View>(_ root: Root, for tab: HubWindowTab) {
        hosts[tab]?.removeFromSuperview()
        let host = NSHostingView(rootView: root)
        // The window's minimum follows the shown half (AppDelegate); a hidden
        // half's SwiftUI minimum must not constrain it.
        host.sizingOptions = []
        host.frame = bounds; host.autoresizingMask = [.width, .height]
        host.isHidden = tab != shown
        addSubview(host)
        hosts[tab] = host
        focus[tab] = nil
    }

    func show(_ tab: HubWindowTab) {
        if let responder = window?.firstResponder as? NSView {
            // A text field's focus lives in the shared field editor; remember the field.
            let owner = (responder as? NSText).flatMap { $0.isFieldEditor ? $0.delegate as? NSView : nil } ?? responder
            for (key, host) in hosts where key != tab && owner.isDescendant(of: host) { focus[key] = Remembered(view: owner) }
        }
        shown = tab
        for (key, host) in hosts { host.isHidden = key != tab }
        guard let window, let host = hosts[tab] else { return }
        if let view = focus[tab]?.view, view.window === window, view.isDescendant(of: host) {
            window.makeFirstResponder(view)
        } else if let responder = window.firstResponder as? NSView, responder.isHiddenOrHasHiddenAncestor {
            window.makeFirstResponder(nil)
        }
    }
}
