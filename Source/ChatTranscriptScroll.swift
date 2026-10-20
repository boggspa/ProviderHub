import AppKit
import SwiftUI

/// Auto-follow is an intention, not a property inferred from changing layout.
/// A user's scroll cancels even an already queued follow before AppKit moves.
@MainActor
final class ChatTranscriptFollow: ObservableObject {
    @Published private(set) var followsLatest = true
    private var pending: DispatchWorkItem?
    private var generation: UInt64 = 0
    private(set) var interacting = false

    func request(_ action: @escaping () -> Void) {
        guard followsLatest, !interacting, pending == nil else { return }
        let token = generation
        let work = DispatchWorkItem { [weak self] in
            guard let self, self.generation == token else { return }
            self.pending = nil
            guard self.followsLatest, !self.interacting else { return }
            // Outside SwiftUI's update/layout transaction, once per frame at
            // most. Never nest scrollTo inside a streaming layout update.
            var transaction = Transaction(animation: nil)
            transaction.disablesAnimations = true
            withTransaction(transaction, action)
        }
        pending = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.0 / 30, execute: work)
    }

    func pause() {
        cancel()
        if followsLatest { followsLatest = false }
    }
    func beginInteraction() { interacting = true; pause() }
    func endInteraction(atBottom: Bool) {
        interacting = false
        if atBottom, !followsLatest { followsLatest = true }
    }
    func resume() {
        cancel(); interacting = false
        if !followsLatest { followsLatest = true }
    }
    func cancel() {
        generation &+= 1
        pending?.cancel(); pending = nil
    }
}

/// Observe the existing SwiftUI scroll view. This adds no second scroll view
/// and never consumes input or changes responder/selection behavior.
@MainActor
struct ChatTranscriptScrollObserver: NSViewRepresentable {
    let follow: ChatTranscriptFollow
    func makeNSView(context: Context) -> Probe { Probe(follow: follow) }
    func updateNSView(_ nsView: Probe, context: Context) { nsView.attach() }
    static func dismantleNSView(_ nsView: Probe, coordinator: ()) { nsView.detach() }

    @MainActor final class Probe: NSView {
        let follow: ChatTranscriptFollow
        private weak var scroll: NSScrollView?
        private var monitor: Any?
        private var notifications: [NSObjectProtocol] = []
        private var wheelEnd: DispatchWorkItem?
        private var liveScroll = false

        init(follow: ChatTranscriptFollow) { self.follow = follow; super.init(frame: .zero) }
        required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
        override func hitTest(_ point: NSPoint) -> NSView? { nil }
        override func viewDidMoveToWindow() {
            super.viewDidMoveToWindow()
            if window == nil { detach() }
            else { attach() }
        }
        override func viewDidMoveToSuperview() {
            super.viewDidMoveToSuperview()
            // SwiftUI may insert the probe before its scroll ancestor.
            DispatchQueue.main.async { [weak self] in self?.attach() }
        }

        func attach() {
            guard window != nil, let enclosing = enclosingScrollView, enclosing !== scroll else { return }
            // The first attachment must retain onAppear's queued initial
            // follow. Reparenting an existing viewport cancels its old target.
            detach(cancelFollow: scroll != nil); scroll = enclosing
            let center = NotificationCenter.default
            notifications.append(center.addObserver(forName: NSScrollView.willStartLiveScrollNotification, object: enclosing, queue: .main) { [weak self] _ in
                MainActor.assumeIsolated { self?.liveScroll = true; self?.follow.beginInteraction() }
            })
            notifications.append(center.addObserver(forName: NSScrollView.didEndLiveScrollNotification, object: enclosing, queue: .main) { [weak self] _ in
                MainActor.assumeIsolated { self?.liveScroll = false; self?.finishInteraction() }
            })
            monitor = NSEvent.addLocalMonitorForEvents(matching: [.scrollWheel, .keyDown, .leftMouseDown]) { [weak self] event in
                MainActor.assumeIsolated { self?.observe(event) }
                return event
            }
        }

        private func observe(_ event: NSEvent) {
            // Code hides rather than leaves the window while Providers shows,
            // so a click or scroll there can land over this transcript's frame.
            guard let scroll, event.window === scroll.window, !scroll.isHiddenOrHasHiddenAncestor else { return }
            if event.type == .keyDown {
                // Page/Home/End/arrows only when focus belongs to this pane.
                guard [115, 116, 119, 121, 125, 126].contains(event.keyCode),
                      let responder = window?.firstResponder as? NSView,
                      responder === scroll || responder.isDescendant(of: scroll) else { return }
            } else {
                let point = scroll.convert(event.locationInWindow, from: nil)
                guard scroll.bounds.contains(point) else { return }
                if event.type == .leftMouseDown {
                    guard let scroller = scroll.verticalScroller,
                          scroller.frame.contains(point) else { return }
                }
            }
            follow.beginInteraction()
            wheelEnd?.cancel()
            let work = DispatchWorkItem { [weak self] in
                guard let self, !self.liveScroll else { return }
                self.finishInteraction()
            }
            wheelEnd = work
            // Also handles wheel mice/key scrolling, which may have no phase.
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.2, execute: work)
        }

        private func finishInteraction() {
            wheelEnd?.cancel(); wheelEnd = nil
            guard let scroll, let document = scroll.documentView else { return }
            let visible = scroll.documentVisibleRect
            let distance = document.isFlipped ? document.bounds.maxY - visible.maxY : visible.minY - document.bounds.minY
            follow.endInteraction(atBottom: distance <= 2)
        }

        func detach(cancelFollow: Bool = true) {
            wheelEnd?.cancel(); wheelEnd = nil
            if let monitor { NSEvent.removeMonitor(monitor) }; monitor = nil
            for token in notifications { NotificationCenter.default.removeObserver(token) }
            notifications.removeAll(); scroll = nil; liveScroll = false
            if cancelFollow { follow.cancel() }
        }
    }
}
