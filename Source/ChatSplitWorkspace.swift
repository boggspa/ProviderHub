import AppKit
import SwiftUI
import UniformTypeIdentifiers

extension UTType {
    static let chatPaneChatID = UTType(exportedAs: "com.mistralbridge.providerhub.chat-id")
}

public enum ChatPaneDropZone: Equatable, Sendable { case left, right, top, bottom, center }

public enum ChatPaneDropGeometry {
    public static func zone(at point: CGPoint, in size: CGSize) -> ChatPaneDropZone {
        let x = min(28, size.width / 4), y = min(28, size.height / 4)
        if point.x < x { return .left }; if point.x > size.width - x { return .right }
        if point.y < y { return .top }; if point.y > size.height - y { return .bottom }
        return .center
    }
    public static func allowed(_ zone: ChatPaneDropZone, size: CGSize) -> Bool {
        switch zone {
        case .left, .right: return size.width >= 848 && size.height >= 300
        case .top, .bottom: return size.height >= 608 && size.width >= 420
        case .center: return true
        }
    }
    public static func preview(_ zone: ChatPaneDropZone, bounds: CGRect) -> CGRect {
        switch zone {
        case .left: return CGRect(x: bounds.minX, y: bounds.minY, width: bounds.width/2, height: bounds.height)
        case .right: return CGRect(x: bounds.midX, y: bounds.minY, width: bounds.width/2, height: bounds.height)
        case .top: return CGRect(x: bounds.minX, y: bounds.minY, width: bounds.width, height: bounds.height/2)
        case .bottom: return CGRect(x: bounds.minX, y: bounds.midY, width: bounds.width, height: bounds.height/2)
        case .center: return bounds
        }
    }
}

@MainActor
public struct ChatSplitWorkspace<Leaf: View>: NSViewControllerRepresentable {
    @Binding var layout: ChatWorkspaceLayout
    @Binding var focusedPaneID: UUID?
    let leafContent: (UUID, ChatPaneAssignment?) -> Leaf
    let onFocus: (UUID) -> Void
    let onDropChat: (UUID, ChatPaneDropZone, String) -> Void
    let focusedOnly: Bool

    public init(layout: Binding<ChatWorkspaceLayout>, focusedPaneID: Binding<UUID?>,
                leafContent: @escaping (UUID, ChatPaneAssignment?) -> Leaf,
                onFocus: @escaping (UUID) -> Void,
                onDropChat: @escaping (UUID, ChatPaneDropZone, String) -> Void,
                focusedOnly: Bool = false) {
        _layout = layout; _focusedPaneID = focusedPaneID
        self.leafContent = leafContent; self.onFocus = onFocus; self.onDropChat = onDropChat
        self.focusedOnly = focusedOnly
    }
    public func makeNSViewController(context: Context) -> ChatNativeWorkspaceController {
        let controller = ChatNativeWorkspaceController(); update(controller); return controller
    }
    public func updateNSViewController(_ controller: ChatNativeWorkspaceController, context: Context) { update(controller) }
    private func update(_ controller: ChatNativeWorkspaceController) {
        controller.update(layout: layout, focused: focusedPaneID, focusedOnly: focusedOnly,
            content: { AnyView(leafContent($0, $1)) }, onFocus: onFocus, onDrop: onDropChat,
            onResize: { id, ratio in layout.setRatio(splitID: id, ratio: ratio) })
    }
}

@MainActor
public final class ChatNativeWorkspaceController: NSViewController {
    private var hosts: [UUID: ChatLeafController] = [:]
    private var splits: [UUID: ChatNativeSplitController] = [:]
    private var signature = ""
    private var mounted: NSViewController?
    private var monitor: Any?
    public override func loadView() { view = NSView(); view.wantsLayer = true }
    deinit { if let monitor { NSEvent.removeMonitor(monitor) } }

    // Exposed internally for executable identity/geometry qualification.
    func leafController(_ id: UUID) -> NSViewController? { hosts[id] }
    func splitController(_ id: UUID) -> NSSplitViewController? { splits[id] }

    func update(layout: ChatWorkspaceLayout, focused: UUID?, focusedOnly: Bool,
                content: (UUID, ChatPaneAssignment?) -> AnyView,
                onFocus: @escaping (UUID) -> Void,
                onDrop: @escaping (UUID, ChatPaneDropZone, String) -> Void,
                onResize: @escaping (UUID, Double) -> Void) {
        _ = view
        if monitor == nil {
            monitor = NSEvent.addLocalMonitorForEvents(matching: [.leftMouseDown, .rightMouseDown]) { [weak self] event in
                guard let self, let window = self.view.window, event.window === window else { return event }
                let point = self.view.convert(event.locationInWindow, from: nil)
                for host in self.hosts.values where host.view.window != nil {
                    if host.view.convert(host.view.bounds, to: self.view).contains(point) { host.focus(); break }
                }
                return event // Never consume the inner control's click.
            }
        }
        let valid = Set(layout.panes.map(\.id))
        for id in Array(hosts.keys) where !valid.contains(id) { hosts[id] = nil }
        for pane in layout.panes {
            let host = hosts[pane.id] ?? ChatLeafController(id: pane.id)
            hosts[pane.id] = host
            host.host.rootView = content(pane.id, pane.assignment)
            host.focus = { onFocus(pane.id) }; host.drop = { zone, chat in onDrop(pane.id, zone, chat) }
            host.surface.focused = layout.panes.count > 1 && pane.id == focused
        }
        func shape(_ node: ChatWorkspaceNode) -> String {
            switch node {
            case .leaf(let pane): return pane.id.uuidString
            case .split(let split): return split.id.uuidString + split.axis.rawValue + "(" + shape(split.first) + "," + shape(split.second) + ")"
            }
        }
        let next = focusedOnly ? "focus:" + (focused ?? layout.focusedPaneID).uuidString : shape(layout.root)
        if next != signature {
            // Detach every parent before rebuilding. Leaf controllers remain cached.
            for split in splits.values { for item in split.splitViewItems { split.removeSplitViewItem(item) } }
            mounted?.view.removeFromSuperview(); mounted?.removeFromParent(); mounted = nil
            splits.removeAll()
            func build(_ node: ChatWorkspaceNode) -> NSViewController {
                switch node {
                case .leaf(let pane): return hosts[pane.id]!
                case .split(let split):
                    let controller = ChatNativeSplitController(id: split.id, axis: split.axis)
                    controller.firstMinimum = minimum(split.first); controller.secondMinimum = minimum(split.second)
                    controller.ratio = split.ratio; controller.changed = onResize
                    for (node,size) in [(split.first,controller.firstMinimum),(split.second,controller.secondMinimum)] {
                        let item = NSSplitViewItem(viewController:build(node))
                        item.minimumThickness = split.axis == .horizontal ? size.width : size.height
                        item.maximumThickness = 100_000
                        controller.addSplitViewItem(item)
                    }
                    splits[split.id] = controller; return controller
                }
            }
            let root = focusedOnly ? hosts[focused ?? layout.focusedPaneID]! : build(layout.root)
            addChild(root); root.view.translatesAutoresizingMaskIntoConstraints = false
            view.addSubview(root.view)
            NSLayoutConstraint.activate([root.view.leadingAnchor.constraint(equalTo: view.leadingAnchor), root.view.trailingAnchor.constraint(equalTo: view.trailingAnchor), root.view.topAnchor.constraint(equalTo: view.topAnchor), root.view.bottomAnchor.constraint(equalTo: view.bottomAnchor)])
            mounted = root; signature = next
        }
        func refresh(_ node: ChatWorkspaceNode) {
            if case .split(let split) = node {
                splits[split.id]?.ratio = split.ratio; splits[split.id]?.changed = onResize
                splits[split.id]?.firstMinimum = minimum(split.first); splits[split.id]?.secondMinimum = minimum(split.second)
                splits[split.id]?.applyRatio()
                refresh(split.first); refresh(split.second)
            }
        }
        refresh(layout.root)
    }
    private func minimum(_ node: ChatWorkspaceNode) -> CGSize {
        switch node {
        case .leaf: return CGSize(width: 420, height: 300)
        case .split(let split):
            let a = minimum(split.first), b = minimum(split.second)
            if split.axis == .horizontal { return CGSize(width:a.width+b.width+8, height:max(a.height,b.height)) }
            return CGSize(width:max(a.width,b.width), height:a.height+b.height+8)
        }
    }
}

@MainActor
private final class ChatLeafController: NSViewController {
    let id: UUID
    let host = NSHostingController(rootView: AnyView(EmptyView()))
    let surface = ChatDropSurface()
    var focus: () -> Void = {}
    var drop: (ChatPaneDropZone, String) -> Void = { _,_ in }
    init(id: UUID) { self.id = id; super.init(nibName:nil,bundle:nil) }
    required init?(coder: NSCoder) { fatalError("init(coder:)") }
    override func loadView() {
        host.sizingOptions = []
        view = surface; surface.drop = { [weak self] zone, chat in self?.drop(zone,chat) }
        for constraint in [view.widthAnchor.constraint(greaterThanOrEqualToConstant:420), view.heightAnchor.constraint(greaterThanOrEqualToConstant:300)] {
            constraint.priority = NSLayoutConstraint.Priority(900); constraint.isActive = true
        }
        addChild(host); host.view.translatesAutoresizingMaskIntoConstraints = false; view.addSubview(host.view)
        NSLayoutConstraint.activate([host.view.leadingAnchor.constraint(equalTo:view.leadingAnchor),host.view.trailingAnchor.constraint(equalTo:view.trailingAnchor),host.view.topAnchor.constraint(equalTo:view.topAnchor),host.view.bottomAnchor.constraint(equalTo:view.bottomAnchor)])
    }
}

@MainActor
private final class ChatDropSurface: NSView {
    override var isFlipped: Bool { true }
    private let previewLayer = CALayer()
    var drop: (ChatPaneDropZone,String) -> Void = { _,_ in }
    var focused = false { didSet { updatePreview() } }
    var zone: ChatPaneDropZone? { didSet { updatePreview() } }
    override init(frame: NSRect) {
        super.init(frame:frame); wantsLayer = true
        previewLayer.zPosition = 1000; layer?.addSublayer(previewLayer)
        registerForDraggedTypes([NSPasteboard.PasteboardType(UTType.chatPaneChatID.identifier)])
    }
    convenience init() { self.init(frame:.zero) }
    required init?(coder:NSCoder) { fatalError("init(coder:)") }
    private func updateZone(_ sender:NSDraggingInfo) -> NSDragOperation {
        guard sender.draggingPasteboard.availableType(from:[NSPasteboard.PasteboardType(UTType.chatPaneChatID.identifier)]) != nil else { zone = nil; return [] }
        let candidate = ChatPaneDropGeometry.zone(at:convert(sender.draggingLocation,from:nil),in:bounds.size)
        zone = ChatPaneDropGeometry.allowed(candidate,size:bounds.size) ? candidate : nil
        return zone == nil ? [] : .copy
    }
    override func draggingEntered(_ sender:NSDraggingInfo) -> NSDragOperation { updateZone(sender) }
    override func draggingUpdated(_ sender:NSDraggingInfo) -> NSDragOperation { updateZone(sender) }
    override func draggingExited(_ sender:NSDraggingInfo?) { zone = nil }
    override func performDragOperation(_ sender:NSDraggingInfo) -> Bool {
        guard updateZone(sender) == .copy, let zone,
              let data = sender.draggingPasteboard.data(forType:NSPasteboard.PasteboardType(UTType.chatPaneChatID.identifier)), data.count <= 4096 else { self.zone = nil; return false }
        self.zone = nil
        let provider = NSItemProvider(item:data as NSData,typeIdentifier:UTType.chatPaneChatID.identifier)
        provider.loadDataRepresentation(forTypeIdentifier:UTType.chatPaneChatID.identifier) { [weak self] payload,_ in
            guard let payload, payload.count <= 4096, let chat = String(data:payload,encoding:.utf8), !chat.isEmpty else { return }
            Task { @MainActor [weak self] in
                guard let self, self.window != nil, ChatPaneDropGeometry.allowed(zone,size:self.bounds.size) else { return }
                self.drop(zone,chat)
            }
        }
        return true
    }
    override func layout() { super.layout();updatePreview() }
    private func updatePreview() {
        CATransaction.begin();CATransaction.setDisableActions(true)
        previewLayer.frame = zone.map { ChatPaneDropGeometry.preview($0,bounds:bounds) } ?? bounds.insetBy(dx:2,dy:2)
        previewLayer.backgroundColor = zone == nil ? nil : NSColor.systemBlue.withAlphaComponent(0.3).cgColor
        previewLayer.borderWidth = focused && zone == nil ? 1.5 : 0
        previewLayer.borderColor = NSColor.controlAccentColor.cgColor
        CATransaction.commit()
    }
}

@MainActor
private final class ChatNativeSplitController: NSSplitViewController {
    let id: UUID
    let axis: ChatWorkspaceAxis
    var firstMinimum = CGSize.zero, secondMinimum = CGSize.zero
    var ratio = 0.5
    var changed: (UUID,Double) -> Void = { _,_ in }
    private var applying = false
    private var lastSize = CGSize.zero
    init(id:UUID,axis:ChatWorkspaceAxis) { self.id=id; self.axis=axis;super.init(nibName:nil,bundle:nil) }
    required init?(coder:NSCoder) { fatalError("init(coder:)") }
    override func loadView() { super.loadView();splitView.isVertical = axis == .horizontal;splitView.dividerStyle = .thick }
    override func viewDidLayout() {
        super.viewDidLayout()
        if splitView.bounds.size != lastSize {
            lastSize = splitView.bounds.size
            DispatchQueue.main.async { [weak self] in self?.applyRatio() }
        }
    }
    func applyRatio() {
        guard !applying,splitViewItems.count == 2 else { return }
        let total = (axis == .horizontal ? splitView.bounds.width : splitView.bounds.height)-splitView.dividerThickness
        guard total > 0 else { return }
        let a = axis == .horizontal ? firstMinimum.width : firstMinimum.height
        let b = axis == .horizontal ? secondMinimum.width : secondMinimum.height
        if splitViewItems[0].minimumThickness != a { splitViewItems[0].minimumThickness = a }
        if splitViewItems[1].minimumThickness != b { splitViewItems[1].minimumThickness = b }
        let position = total >= a+b ? min(max(total*CGFloat(ratio),a),total-b) : total*CGFloat(ratio)
        let frame = splitViewItems[0].viewController.view.frame
        let current = axis == .horizontal ? frame.width : frame.height
        if abs(current-position) > 0.5 { applying=true;splitView.setPosition(position,ofDividerAt:0);applying=false }
    }
    override func splitViewDidResizeSubviews(_ notification:Notification) {
        super.splitViewDidResizeSubviews(notification)
        guard !applying,splitViewItems.count == 2 else { return }
        if splitView.bounds.size != lastSize {
            lastSize = splitView.bounds.size
            DispatchQueue.main.async { [weak self] in self?.applyRatio() }
            return
        }
        guard [.leftMouseDown,.leftMouseDragged,.leftMouseUp].contains(NSApp.currentEvent?.type) else { return }
        let total = (axis == .horizontal ? splitView.bounds.width : splitView.bounds.height)-splitView.dividerThickness
        guard total > 0 else { return }
        let frame = splitViewItems[0].viewController.view.frame
        let first = axis == .horizontal ? frame.width : frame.height
        ratio=Double(first/total); changed(id,ratio)
    }
}
