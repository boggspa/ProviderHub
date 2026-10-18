"""Execute the production Swift multiview layout rules without opening an app."""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

CASES = r'''
import Foundation
@main struct Cases {
    static func main() {
        var layout = ChatWorkspaceLayout.initial(chatID: "chat-a")
        precondition(layout.panes.count == 1)
        let first = layout.panes[0].id
        precondition(layout.focusedPaneID == first)
        layout.splitRight(first)
        precondition(layout.panes.count == 2)
        let second = layout.focusedPaneID
        precondition(second != first)
        layout.assignChat(paneID: second, chatID: "chat-b")
        if case .some(.chat(let id)) = layout.assignment(for: second) {
            precondition(id == "chat-b")
        } else {
            preconditionFailure("assignChat did not stick")
        }
        layout.splitBelow(second)
        precondition(layout.panes.count == 3)
        layout.setRatio(splitID: layout.focusedPaneID, ratio: 0.5)
        var deep = layout
        for i in 0..<8 {
            deep.splitRight(deep.focusedPaneID, assignment: .chat(id: "c-\(i)"))
        }
        precondition(deep.panes.count == 11, "nesting must be unlimited by count")
        var clamped = layout
        if case .split(let s) = clamped.root { clamped.setRatio(splitID: s.id, ratio: 99) }
        if case .split(let s) = clamped.root { precondition(s.ratio == 0.8, "ratio must clamp to 0.8") }
        else { preconditionFailure("expected split root") }
        let idsBefore = Set(layout.panes.map(\.id))
        let data = try! layout.save()
        let restored = try! ChatWorkspaceLayout.load(from: data)
        precondition(restored == layout, "Codable round-trip must preserve tree")
        precondition(Set(restored.panes.map(\.id)) == idsBefore, "UUIDs must be stable across persistence")
        var closed = layout
        let untouched = closed
        precondition(!closed.close(paneID: UUID()))
        precondition(closed == untouched, "unknown close must leave layout unchanged")
        let victim = closed.panes[0].id
        precondition(closed.close(paneID: victim))
        precondition(closed.panes.count == 2, "close must collapse sibling up")
        precondition(!closed.panes.contains { $0.id == victim })
        precondition(ChatWorkspaceLayout.maxPanes(total: 1000) >= 2)
        precondition(ChatWorkspaceLayout.minimumPaneSize >= 200, "minimum size must enforce usable bounds")
        var duplicate = layout
        let repeated = ChatWorkspacePane(id:first)
        duplicate.root = .split(ChatWorkspaceSplit(axis:.horizontal,first:.leaf(repeated),second:.leaf(repeated)))
        precondition((try? ChatWorkspaceLayout.load(from: duplicate.save())) == nil,"duplicate UUIDs rejected")
        var invalidRatio = layout
        if case .split(var split) = invalidRatio.root { split.ratio = 0.99; invalidRatio.root = .split(split) }
        precondition((try? ChatWorkspaceLayout.load(from:invalidRatio.save())) == nil,"invalid ratio rejected")
        var overdeep = ChatWorkspaceLayout.initial(chatID:"a")
        for _ in 0..<34 { overdeep.splitRight(overdeep.focusedPaneID) }
        precondition((try? ChatWorkspaceLayout.load(from:overdeep.save())) == nil,"malformed persistence depth rejected")
        // Open in Split View: the new pane lands on the edge asked for.
        var edges = ChatWorkspaceLayout.initial(chatID: "a")
        let anchor = edges.focusedPaneID
        edges.split(anchor, edge: .left, assignment: .chat(id: "left"))
        precondition(edges.panes.map(\.assignment) == [.chat(id: "left"), .chat(id: "a")], "left split leads")
        precondition(edges.focusedPaneID == edges.pane(showing: "left"), "focus moves to the opened pane")
        edges.split(anchor, edge: .top, assignment: .chat(id: "top"))
        precondition(edges.panes.map(\.assignment) == [.chat(id: "left"), .chat(id: "top"), .chat(id: "a")], "top split leads")
        edges.split(anchor, edge: .bottom, assignment: .chat(id: "bottom"))
        precondition(edges.panes.map(\.assignment).last == .chat(id: "bottom"), "bottom split follows")
        precondition(edges.pane(showing: "a") == anchor && edges.pane(showing: "missing") == nil)
        // Next and previous pane wrap in reading order.
        edges.focus(paneID: anchor)
        let order = edges.panes.map(\.id)
        precondition(edges.adjacentPane(1) == order[3] && edges.adjacentPane(-1) == order[1])
        edges.focus(paneID: order[3])
        precondition(edges.adjacentPane(1) == order[0], "next wraps to the first pane")
        edges.focus(paneID: order[0])
        precondition(edges.adjacentPane(-1) == order[3], "previous wraps to the last pane")
        var solo = ChatWorkspaceLayout.initial(chatID: "solo")
        precondition(solo.adjacentPane(1) == solo.focusedPaneID, "one pane has no neighbour")
        solo.split(solo.focusedPaneID, edge: .right)
        precondition((try? ChatWorkspaceLayout.load(from: edges.save())) == edges, "edge splits persist")
        print("multiview cases passed")
    }
}
'''

@unittest.skipUnless(sys.platform == "darwin" and shutil.which("xcrun"), "native Swift requires macOS")
class MultiviewTests(unittest.TestCase):
    def test_native_renderer_identity_resize_and_drop_geometry(self):
        source = Path(__file__).parent
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cases = root / "Renderer.swift"
            cases.write_text(RENDERER)
            binary = root / "renderer"
            result = subprocess.run(["xcrun", "swiftc", "-parse-as-library", str(source / "ChatWorkspaceLayout.swift"), str(source / "ChatSplitWorkspace.swift"), str(cases), "-o", str(binary)], capture_output=True,text=True,timeout=120)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            result = subprocess.run([str(binary)],capture_output=True,text=True,timeout=30)
            print(result.stdout)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    def test_production_multiview_split_close_size_persistence(self):
        source = Path(__file__).parent
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cases = root / "Cases.swift"
            cases.write_text(CASES)
            binary = root / "cases"
            result = subprocess.run(["xcrun", "swiftc", "-parse-as-library", "-module-cache-path",
                str(root / "cache"), str(source / "ChatWorkspaceLayout.swift"), str(cases), "-o", str(binary)],
                capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("multiview cases passed", result.stdout)

class MultiviewAffordanceTests(unittest.TestCase):
    """Claude Desktop's split view, reachable without dragging: the sidebar
    opens a chat beside the focused pane and marks the chats on screen."""

    def test_sidebar_opens_chats_in_split_view_and_marks_open_ones(self):
        source = Path(__file__).parent
        rail = (source / "ChatWorkspaces.swift").read_text()
        self.assertIn('Menu("Open in")', rail)
        self.assertIn('Button("Split View") { paneOpener.beside(chat.id) }', rail)
        self.assertIn('Button("Split View Below") { paneOpener.below(chat.id) }', rail)
        self.assertIn("paneOpener.split, paneOpener.openChats.contains(chat.id)", rail)
        window = (source / "ChatWindow.swift").read_text()
        self.assertIn("workspace.open(chat: chat, edge: below ? .bottom : .right, model: model)", window)

    def test_panes_carry_a_header_menu_and_keyboard_navigation(self):
        source = Path(__file__).parent
        ui = (source / "ChatWorkspaceUI.swift").read_text()
        header = ui.split("private func paneHeader", 1)[1].split("private func paneTitle", 1)[0]
        for action in ('"Split Right"', '"Split Below"', '"Maximize Pane"', '"Close Pane"'):
            self.assertIn(action, header)
        menu = (source / "ChatFonts.swift").read_text()
        self.assertIn('("Next Pane", "next", "]"', menu)
        self.assertIn('("Previous Pane", "previous", "["', menu)
        self.assertIn('case "next": workspace.focusAdjacent(1, model: model)', ui)


RENDERER = r'''
import AppKit
import SwiftUI
import UniformTypeIdentifiers
@main struct Renderer {
 @MainActor static func main() {
  NSApplication.shared.setActivationPolicy(.prohibited)
  let controller = ChatNativeWorkspaceController()
  let window = NSWindow(contentRect:NSRect(x:0,y:0,width:1400,height:1000),styleMask:[.titled,.resizable],backing:.buffered,defer:false)
  window.contentViewController=controller
  controller.view.frame = NSRect(x:0,y:0,width:1400,height:1000)
  var layout = ChatWorkspaceLayout.initial(chatID:"a")
  let a = layout.focusedPaneID
  func check(_ ok:Bool,_ label:String) { if !ok { print("FAIL "+label);exit(1) }; print("PASS "+label) }
  var changed:[UUID:Double] = [:]
  func update(_ focus:Bool=false) {
   controller.update(layout:layout,focused:layout.focusedPaneID,focusedOnly:focus,
    content:{ id,_ in AnyView(Text(id.uuidString)) },onFocus:{_ in},onDrop:{_,_,_ in},onResize:{changed[$0]=$1})
   window.setContentSize(NSSize(width:1400,height:1000))
   controller.view.layoutSubtreeIfNeeded()
   window.displayIfNeeded()
   RunLoop.current.run(until:Date().addingTimeInterval(0.05))
  }
  update()
  let original = controller.leafController(a)!
  let originalHosting = original.children.first!
  layout.splitRight(a,assignment:.chat(id:"b"));let b=layout.focusedPaneID
  update()
  check(controller.leafController(a) === original,"identity after horizontal split")
  guard case .split(let horizontal)=layout.root else { exit(2) }
  let horizontalController=controller.splitController(horizontal.id)!
  let horizontalView=horizontalController.splitView
  check(horizontalView.isVertical,"horizontal means side by side")
  let initial=horizontalView.subviews[0].frame.width
  check(abs(initial/(horizontalView.bounds.width-horizontalView.dividerThickness)-0.5)<0.02,"restored horizontal ratio")
  horizontalView.setPosition(initial-100,ofDividerAt:0)
  horizontalView.layoutSubtreeIfNeeded()
  check(horizontalView.subviews[0].frame.width < initial-50,"horizontal divider changes width")
  check(horizontalController.splitViewItems[0].minimumThickness==420,"minimum leaf width")
  layout.splitBelow(b,assignment:.chat(id:"c"));update()
  check(controller.leafController(a) === original,"identity after nested vertical split")
  guard case .split(let outer)=layout.root,case .split(let vertical)=outer.second else {exit(3)}
  let verticalController=controller.splitController(vertical.id)!
  let verticalView=verticalController.splitView
  check(!verticalView.isVertical,"vertical means top and bottom")
  let oldHeight=verticalView.subviews[0].frame.height
  verticalView.setPosition(oldHeight-80,ofDividerAt:0)
  verticalView.layoutSubtreeIfNeeded()
  check(abs(verticalView.subviews[0].frame.height-oldHeight)>40,"vertical divider changes height")
  check(verticalController.splitViewItems[0].minimumThickness==300,"minimum leaf height")
  update(true);check(controller.leafController(a) === original,"identity in focused mode")
  update(false);check(controller.leafController(a) === original,"identity restored after focus")
  check(controller.leafController(a)?.children.first === originalHosting,"same NSHostingController after reparenting")
  layout.splitRight(a,assignment:.chat(id:"d"));update()
  check(controller.splitController(horizontal.id)!.splitViewItems[0].minimumThickness==848,"recursive subtree width minimum")
  check(ChatPaneDropGeometry.preview(.left,bounds:CGRect(x:0,y:0,width:1000,height:600)).width==500,"half pane preview")
  check(!ChatPaneDropGeometry.allowed(.right,size:CGSize(width:700,height:700)),"invalid horizontal split suppressed")
  check(!ChatPaneDropGeometry.allowed(.bottom,size:CGSize(width:900,height:500)),"invalid vertical split suppressed")
  let provider=NSItemProvider(item:Data("chat-fixture".utf8) as NSData,typeIdentifier:UTType.chatPaneChatID.identifier)
  var received:String?
  provider.loadDataRepresentation(forTypeIdentifier:UTType.chatPaneChatID.identifier){data,_ in received=data.flatMap{String(data:$0,encoding:.utf8)}}
  let deadline=Date().addingTimeInterval(2)
  while received == nil && Date()<deadline {RunLoop.current.run(until:Date().addingTimeInterval(0.01))}
  check(received=="chat-fixture","custom UTI data representation")
  print("native renderer cases passed");exit(0)
 }
}
'''
