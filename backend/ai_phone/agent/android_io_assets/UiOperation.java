package org.aiphone.observation;

import android.app.UiAutomation;
import android.accessibilityservice.AccessibilityServiceInfo;
import android.os.Build;
import android.os.Bundle;
import android.os.SystemClock;
import android.view.accessibility.AccessibilityNodeInfo;
import android.view.accessibility.AccessibilityWindowInfo;
import java.io.BufferedReader;
import java.io.File;
import java.io.FileInputStream;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.util.ArrayDeque;
import org.json.JSONObject;

/**
 * One request on stdin, one result on stdout, using Android's built-in uiautomator framework JAR.
 * Rebuild in this directory with an Android SDK platform JAR and build-tools d8:
 *   javac --release 8 -cp "$ANDROID_JAR" -d "$BUILD" UiOperation.java ObservationTree.java
 *   d8 --min-api 21 --lib "$ANDROID_JAR" --output ui-operation.jar "$BUILD"/org/aiphone/observation/*.class
 * The framework bridge is the one used by Android's uiautomator command.
 */
public final class UiOperation {
    private static boolean isEditor(AccessibilityNodeInfo node, boolean resolvedByFocus) {
        if (node == null || !node.isFocused() || !node.isEnabled()) return false;
        for (AccessibilityNodeInfo.AccessibilityAction action : node.getActionList()) {
            if (action.getId() == AccessibilityNodeInfo.ACTION_SET_TEXT) return true;
        }
        // Some older editors omit their action list but have explicit input focus and editable semantics.
        return resolvedByFocus && node.isEditable();
    }

    private static AccessibilityNodeInfo scanFocusedEditor(AccessibilityNodeInfo root, long end) {
        ArrayDeque<AccessibilityNodeInfo> pending = new ArrayDeque<>();
        pending.add(root);
        AccessibilityNodeInfo found = null;
        int visited = 0;
        while (!pending.isEmpty()) {
            if (SystemClock.uptimeMillis() >= end || ++visited > 1024) return null;
            AccessibilityNodeInfo node = pending.removeFirst();
            if (isEditor(node, false)) {
                if (found != null && !found.equals(node)) return null;
                found = node;
            }
            int count = node.getChildCount();
            for (int i = 0; i < count; i++) {
                if (visited + pending.size() >= 1024 || SystemClock.uptimeMillis() >= end) return null;
                AccessibilityNodeInfo child = node.getChild(i);
                if (child != null) pending.addLast(child);
            }
        }
        return found != null && found.refresh() && isEditor(found, false) ? found : null;
    }

    private static AccessibilityNodeInfo focusedEditor(UiAutomation automation) {
        long end = SystemClock.uptimeMillis() + 900;
        do {
            AccessibilityNodeInfo root = automation.getRootInActiveWindow();
            if (root != null) {
                AccessibilityNodeInfo field = root.findFocus(AccessibilityNodeInfo.FOCUS_INPUT);
                if (field == null) field = automation.findFocus(AccessibilityNodeInfo.FOCUS_INPUT);
                if (field != null && field.getWindowId() == root.getWindowId()
                        && field.refresh() && isEditor(field, true)) return field;
                // A newly attached service can see the focused node before its global focus cache is populated.
                field = scanFocusedEditor(root, end);
                if (field != null) return field;
            }
            if (SystemClock.uptimeMillis() < end) SystemClock.sleep(40);
        } while (SystemClock.uptimeMillis() < end);
        return null;
    }

    private static void setText(UiAutomation automation, String text) throws Exception {
        AccessibilityNodeInfo field = focusedEditor(automation);
        if (field == null) {
            System.out.println("AIPHONE_NO_FOCUSED_EDITOR");
            return;
        }
        Bundle args = new Bundle();
        args.putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text);
        if (!field.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args)) {
            System.out.println("AIPHONE_SET_TEXT_REJECTED");
            return;
        }
        // Password fields deliberately conceal their value. The accessibility action is authoritative there.
        if (!field.isPassword()) {
            long end = SystemClock.uptimeMillis() + 500;
            do {
                if (field.refresh()) {
                    CharSequence actual = field.getText();
                    boolean emptyWithHint = text.isEmpty() && Build.VERSION.SDK_INT >= 26 && field.isShowingHintText();
                    if (emptyWithHint || text.contentEquals(actual == null ? "" : actual)) {
                        System.out.println("AIPHONE_TEXT_SET");
                        return;
                    }
                }
                SystemClock.sleep(30);
            } while (SystemClock.uptimeMillis() < end);
            System.out.println("AIPHONE_TEXT_UNCONFIRMED");
            return;
        }
        System.out.println("AIPHONE_TEXT_SET");
    }

    private static boolean dump(UiAutomation automation, JSONObject options) throws Exception {
        AccessibilityNodeInfo root = automation.getRootInActiveWindow();
        if (root == null) {
            AccessibilityServiceInfo info = automation.getServiceInfo();
            if (info != null) {
                info.flags |= AccessibilityServiceInfo.FLAG_RETRIEVE_INTERACTIVE_WINDOWS;
                automation.setServiceInfo(info);
                for (AccessibilityWindowInfo window : automation.getWindows()) {
                    if (window.isActive() && window.isFocused()) {
                        root = window.getRoot();
                        if (root != null) break;
                    }
                }
            }
        }
        for (int i = 0; root == null && i < 4; i++) {
            SystemClock.sleep(50);
            root = automation.getRootInActiveWindow();
        }
        if (root == null) {
            System.err.println("AIPHONE_TREE_NO_ROOT");
            return false;
        }
        File xml = new File(options.getString("output"));
        try {
            ObservationTree.write(root, xml, options.getInt("rotation"),
                                  options.getInt("width"), options.getInt("height"));
            if (!xml.isFile() || xml.length() == 0) {
                System.err.println(xml.isFile() ? "AIPHONE_TREE_EMPTY_FILE" : "AIPHONE_TREE_NO_FILE");
                return false;
            }
            try (FileInputStream stream = new FileInputStream(xml)) {
                byte[] buffer = new byte[16384];
                int count;
                while ((count = stream.read(buffer)) != -1) System.out.write(buffer, 0, count);
                System.out.flush();
            }
            return true;
        } finally {
            root.recycle();
            xml.delete();  // The invoking shell and host also clean their own randomized path.
        }
    }

    public static void main(String[] args) {
        int status = 1;
        try {
            BufferedReader input = new BufferedReader(new InputStreamReader(System.in, StandardCharsets.UTF_8));
            JSONObject request = new JSONObject(input.readLine());
            Class<?> wrapperClass = Class.forName("com.android.uiautomator.core.UiAutomationShellWrapper");
            Object wrapper = wrapperClass.getDeclaredConstructor().newInstance();
            wrapperClass.getMethod("connect").invoke(wrapper);
            try {
                UiAutomation automation = (UiAutomation) wrapperClass.getMethod("getUiAutomation").invoke(wrapper);
                if ("input".equals(request.getString("action"))) {
                    try {
                        wrapperClass.getMethod("setCompressedLayoutHierarchy", boolean.class).invoke(wrapper, false);
                    } catch (NoSuchMethodException ignored) {
                        // Use the service's normal active hierarchy on older vendor bridges.
                    }
                    setText(automation, request.getString("text"));
                    status = 0;
                } else if ("tree".equals(request.getString("action"))) {
                    wrapperClass.getMethod("setCompressedLayoutHierarchy", boolean.class).invoke(wrapper, false);
                    status = dump(automation, request) ? 0 : 2;
                } else {
                    throw new IllegalArgumentException();
                }
            } finally {
                wrapperClass.getMethod("disconnect").invoke(wrapper);
            }
        } catch (Exception failure) {
            // Node text and the caller's input are never printed on an error path.
            Throwable cause = failure.getCause() == null ? failure : failure.getCause();
            System.err.println("AIPHONE_UI_ERROR " + cause.getClass().getSimpleName());
        }
        System.exit(status);
    }
}
