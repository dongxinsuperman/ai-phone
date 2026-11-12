#import <Foundation/Foundation.h>
#import <IOKit/hid/IOHIDUsageTables.h>
#import <dlfcn.h>
#import <unistd.h>
#import <xpc/xpc.h>

// CoreSimulator routes these button events to the simulator's system UI, including the Duo inner screen.
// The guest service and message format are documented by idb's DTUHID transport.
static const char *digitizerService = "com.apple.coredevice.feature.remote.hid.digitizer";

@interface NSObject (DuoCoreSimulator)
+ (id)sharedServiceContextForDeveloperDir:(NSString *)developerDir error:(NSError **)error;
- (id)defaultDeviceSetWithError:(NSError **)error;
- (NSArray *)devices;
- (NSUUID *)UDID;
- (mach_port_t)lookup:(NSString *)service error:(NSError **)error;
@end

static xpc_connection_t connectDigitizer(NSString *developerDir, NSUUID *udid) {
    if (!dlopen("/Library/Developer/PrivateFrameworks/CoreSimulator.framework/CoreSimulator", RTLD_NOW)) {
        fprintf(stderr, "Cannot load CoreSimulator: %s\n", dlerror());
        return nil;
    }
    xpc_endpoint_t (*makeEndpoint)(mach_port_t, uint64_t, uint64_t) =
        dlsym(RTLD_DEFAULT, "xpc_endpoint_create_mach_port_4sim");
    void (*enableSimulator)(xpc_connection_t) = dlsym(RTLD_DEFAULT, "xpc_connection_enable_sim2host_4sim");
    if (!makeEndpoint || !enableSimulator) {
        fputs("CoreSimulator's HID connection is unavailable\n", stderr);
        return nil;
    }

    NSError *error = nil;
    id context = [NSClassFromString(@"SimServiceContext") sharedServiceContextForDeveloperDir:developerDir error:&error];
    id deviceSet = [context defaultDeviceSetWithError:&error];
    for (id device in [deviceSet devices]) {
        if (![[device UDID] isEqual:udid]) continue;
        mach_port_t port = [device lookup:@(digitizerService) error:&error];
        if (port == MACH_PORT_NULL) break;
        xpc_endpoint_t endpoint = makeEndpoint(port, 0, 0);
        if (!endpoint) {
            mach_port_deallocate(mach_task_self(), port);
            break;
        }
        xpc_connection_t connection = xpc_connection_create_from_endpoint(endpoint);
        if (!connection) break;
        enableSimulator(connection);
        return connection;
    }
    fprintf(stderr, "Cannot connect to the simulator's digitizer: %s\n",
            error ? error.localizedDescription.UTF8String : "device or service not found");
    return nil;
}

static xpc_object_t message(const char *type, xpc_object_t payload, bool barrier) {
    xpc_object_t request = xpc_dictionary_create_empty();
    xpc_dictionary_set_string(request, "featureIdentifier", digitizerService);
    xpc_dictionary_set_string(request, "messageType", type);
    xpc_dictionary_set_bool(request, "isBarrier", barrier);
    xpc_dictionary_set_value(request, "payload", payload);
    return request;
}

static BOOL waitForHID(xpc_connection_t connection) {
    // Keyboard usage zero is a no-op and can be used to check that the simulator received the events.
    xpc_object_t key = xpc_dictionary_create_empty();
    xpc_dictionary_set_uint64(key, "usageCode", 0);
    xpc_dictionary_set_uint64(key, "state", 2);
    dispatch_semaphore_t done = dispatch_semaphore_create(0);
    __block BOOL received = NO;
    xpc_connection_send_message_with_reply(connection, message("IndigoKeyboardButtonEvent", key, true),
        dispatch_get_global_queue(QOS_CLASS_DEFAULT, 0), ^(xpc_object_t reply) {
            received = xpc_get_type(reply) == XPC_TYPE_DICTIONARY;
            dispatch_semaphore_signal(done);
        });
    return dispatch_semaphore_wait(done, dispatch_time(DISPATCH_TIME_NOW, 3 * NSEC_PER_SEC)) == 0 && received;
}

static void sendHomeButton(xpc_connection_t connection, BOOL pressed) {
    xpc_object_t button = xpc_dictionary_create_empty();
    xpc_dictionary_set_uint64(button, "usagePage", kHIDPage_Consumer);
    xpc_dictionary_set_uint64(button, "usageCode", kHIDUsage_Csmr_Menu);
    xpc_dictionary_set_uint64(button, "state", pressed ? 1 : 2);
    xpc_connection_send_message(connection, message("IndigoButtonEvent", button, false));
}

int main(int argc, char **argv) {
    @autoreleasepool {
        if (argc != 3) {
            fputs("Expected Xcode developer directory and simulator UDID\n", stderr);
            return 2;
        }
        NSUUID *udid = [[NSUUID alloc] initWithUUIDString:@(argv[2])];
        if (!udid) {
            fputs("Invalid simulator UDID\n", stderr);
            return 2;
        }
        xpc_connection_t connection = connectDigitizer(@(argv[1]), udid);
        if (!connection) return 1;
        xpc_connection_set_event_handler(connection, ^(xpc_object_t event) {});
        xpc_connection_activate(connection);
        if (!waitForHID(connection)) {
            xpc_connection_cancel(connection);
            fputs("Simulator did not acknowledge the HID connection\n", stderr);
            return 1;
        }
        usleep(200000);
        sendHomeButton(connection, YES);
        usleep(60000);
        sendHomeButton(connection, NO);
        usleep(140000);
        sendHomeButton(connection, YES);
        usleep(60000);
        sendHomeButton(connection, NO);
        BOOL received = waitForHID(connection);
        xpc_connection_cancel(connection);
        if (!received) fputs("Simulator did not acknowledge the App Switcher input\n", stderr);
        return received ? 0 : 1;
    }
}
