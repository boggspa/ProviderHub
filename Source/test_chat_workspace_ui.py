"""Compile and execute the production workspace controller with a model stub."""
from pathlib import Path
import platform
import subprocess
import tempfile
import unittest

STUB = r'''
import SwiftUI
struct ChatSummary { var id:String }
@MainActor final class ChatModel {
 var connected = true
 var chats = [ChatSummary(id:"a"),ChatSummary(id:"b")]
 var selectedID:String? = "a"
 var focused:String? = "a"
 var observed:Set<String> = []
 var requested:[String] = []
 func setPaneFocus(_ id:String?) { focused = id }
 func select(_ id:String) { requested.append(id) }
 func observe(chats:Set<String>) { observed = chats }
}
@main struct Cases {
 @MainActor static func main() {
  let key = "chatWorkspaceLayoutV1"
  let old = UserDefaults.standard.object(forKey:key)
  defer { if let old { UserDefaults.standard.set(old,forKey:key) } else { UserDefaults.standard.removeObject(forKey:key) } }
  var failed = 0
  func check(_ ok:Bool,_ label:String) { print((ok ? "PASS " : "FAIL ")+label); if !ok { failed += 1 } }
  func restored(_ assignment:ChatPaneAssignment?) -> (ChatWorkspaceController,UUID) {
   var layout = ChatWorkspaceLayout.initial(chatID:"a")
   layout.splitRight(layout.focusedPaneID,assignment:assignment)
   let focused = layout.focusedPaneID
   UserDefaults.standard.set(try! layout.save(),forKey:key)
   return (ChatWorkspaceController(),focused)
  }
  for (assignment,label) in [(ChatPaneAssignment.chat(id:"b"),"chat"),(.browser(surface:"b"),"browser")] {
   let (controller,pane) = restored(assignment)
   let model = ChatModel()
   controller.reconcile(model)
   check(controller.layout.focusedPaneID == pane,"restore focused "+label)
   check(controller.layout.panes.first(where:{$0.id == pane})?.assignment == assignment,"restore assignment "+label)
   check(model.requested.last == "b","restore selects saved "+label+" owner")
   controller.selected("a",model:model)
   check(controller.layout.focusedPaneID == pane,"ignore mismatched startup selection "+label)
   model.selectedID = "b"; controller.selected("b",model:model)
   check(controller.layout.focusedPaneID == pane,"matching restoration acknowledgement "+label)
  }
  let (empty,pane) = restored(nil)
  let model = ChatModel()
  empty.reconcile(model)
  check(empty.layout.focusedPaneID == pane,"restore empty focus")
  check(empty.layout.panes.first(where:{$0.id == pane})?.assignment == nil,"restore empty assignment")
  check(model.focused == nil,"empty disables chat shortcuts")
  empty.reconcile(model)
  check(empty.layout.panes.first(where:{$0.id == pane})?.assignment == nil,"background publish preserves restored empty pane")
  // A browser picker must use focus() after assignment; exercise that admission.
  empty.layout.assignBrowser(paneID:pane,surface:"a")
  empty.focus(pane,model:model)
  check(model.focused == nil,"browser picker disables chat shortcuts")
  let closed = ChatWorkspaceController()
  closed.layout = .initial(chatID:"a")
  let closeID = closed.layout.focusedPaneID
  closed.close(closeID,model:model)
  check(closed.layout.panes[0].assignment == nil,"close leaves empty pane")
  closed.reconcile(model)
  check(closed.layout.panes[0].assignment == nil,"background publish preserves closed empty pane")
  let navigation = ChatWorkspaceController()
  navigation.layout = .initial(chatID:"a")
  let firstPane = navigation.layout.focusedPaneID
  navigation.layout.splitRight(firstPane,assignment:.chat(id:"b"))
  let secondPane = navigation.layout.focusedPaneID
  navigation.focus(firstPane,model:model)
  check(navigation.keyboardFocusRequest == nil,"ordinary focus does not request a caret move")
  navigation.focusAdjacent(1,model:model)
  check(navigation.layout.focusedPaneID == secondPane && model.focused == "b","next selects the destination chat")
  check(navigation.keyboardFocusRequest?.pane == secondPane,"next requests destination keyboard focus")
  let previousToken = navigation.keyboardFocusRequest?.token
  navigation.focus(firstPane,model:model)
  navigation.focusAdjacent(1,model:model)
  check(navigation.keyboardFocusRequest?.token != previousToken,"repeat destination emits a fresh keyboard request")
  navigation.focusAdjacent(-1,model:model)
  check(navigation.layout.focusedPaneID == firstPane && navigation.keyboardFocusRequest?.pane == firstPane,
        "previous selects and requests the previous pane")
  exit(failed == 0 ? 0 : 1)
 }
}
'''

@unittest.skipUnless(platform.system() == "Darwin", "requires SwiftUI/AppKit")
class WorkspaceControllerTests(unittest.TestCase):
    def test_production_controller_restoration_focus_and_close(self):
        source = Path(__file__).parent
        # Compile the exact controller; view dependencies are irrelevant to state.
        controller = (source / "ChatWorkspaceUI.swift").read_text().split("struct ChatWorkspaceContent:", 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Controller.swift").write_text(controller)
            (root / "Cases.swift").write_text(STUB)
            # Drop zones are shared UI vocabulary, not a replacement controller.
            (root / "DropZone.swift").write_text("enum ChatPaneDropZone { case left,right,top,bottom,center }\n")
            binary = root / "cases"
            compiled = subprocess.run(["xcrun", "swiftc", "-parse-as-library", str(source / "ChatWorkspaceLayout.swift"), str(root / "Controller.swift"), str(root / "DropZone.swift"), str(root / "Cases.swift"), "-o", str(binary)], capture_output=True, text=True, timeout=120)
            self.assertEqual(compiled.returncode, 0, compiled.stdout + compiled.stderr)
            result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20)
            print(result.stdout)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
