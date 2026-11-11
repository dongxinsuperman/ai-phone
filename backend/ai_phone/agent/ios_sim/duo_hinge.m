// Device Hub's simulator HID event and CoreMotion readback:
// https://github.com/artemnovichkov/hinge
// Sweep through the intermediate angles so iOS switches the active screen:
// https://github.com/skarol/duoctl
#import <Foundation/Foundation.h>
#include <mach/mach_time.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

typedef const void *HIDClient;
typedef const void *HIDEvent;
extern HIDClient IOHIDEventSystemClientCreateWithType(CFAllocatorRef, int, CFDictionaryRef);
extern HIDEvent IOHIDEventCreateVendorDefinedEvent(CFAllocatorRef, uint64_t, uint32_t, uint32_t,
                                                  uint32_t, const uint8_t *, CFIndex, uint32_t);
extern void IOHIDEventSystemClientDispatchEvent(HIDClient, HIDEvent);
extern CFDataRef IOCFSerialize(CFTypeRef, CFOptionFlags);
extern int duoTouch(int argc, char **argv);

@interface CMAngle : NSObject
- (float)angleDegrees;
- (float)mechanicalAngleDegrees;
@end

@interface CMAngleManager : NSObject
+ (BOOL)isAvailable;
- (void)startAngleUpdatesToQueue:(NSOperationQueue *)queue handler:(void (^)(CMAngle *))handler;
- (void)stopAngleUpdates;
@end

static BOOL sendState(HIDClient client, NSString *source, NSString *type, id value, NSString *provider) {
    NSDictionary *payload = @{
        @"provider": provider,
        @"source": source,
        @"type": type,
        @"value": value,
    };
    CFDataRef data = IOCFSerialize((__bridge CFDictionaryRef)payload, 1);
    if (!data) {
        return NO;
    }
    HIDEvent event = IOHIDEventCreateVendorDefinedEvent(NULL, mach_absolute_time(), 0xFF61, 0x5B, 0,
                                                       CFDataGetBytePtr(data), CFDataGetLength(data), 0);
    CFRelease(data);
    if (!event) {
        return NO;
    }
    IOHIDEventSystemClientDispatchEvent(client, event);
    CFRelease(event);
    return YES;
}

static BOOL setOrientation(NSString *value) {
    HIDClient client = IOHIDEventSystemClientCreateWithType(NULL, 4, NULL);
    if (!client) {
        return NO;
    }
    BOOL first = sendState(client, @"orientation-picker-control", @"enum", value,
                           @"com.apple.Virtualization");
    BOOL second = sendState(client, @"orientation-picker-control", @"enum", value,
                            @"com.apple.Virtualization.VirtualMachines");
    usleep(40000);
    CFRelease(client);
    return first && second;
}

static BOOL moveHinge(double start, double target) {
    HIDClient client = IOHIDEventSystemClientCreateWithType(NULL, 4, NULL);
    if (!client) {
        return NO;
    }
    const int steps = 36;
    BOOL sent = YES;
    for (int step = 1; step <= steps; ++step) {
        double angle = start + (target - start) * step / steps;
        if (!sendState(client, @"hinge-slider-control", @"range", @(angle),
                       @"com.apple.Virtualization.VirtualMachines")) {
            sent = NO;
            break;
        }
        usleep(16667);
    }
    CFRelease(client);
    return sent;
}

int main(int argc, char **argv) {
    @autoreleasepool {
        if (argc >= 2 && !strcmp(argv[1], "touch")) {
            return duoTouch(argc - 2, argv + 2);
        }
        if (argc == 3 && !strcmp(argv[1], "orientation")) {
            return setOrientation([NSString stringWithUTF8String:argv[2]]) ? 0 : 1;
        }
        const BOOL replay = argc == 3 && !strcmp(argv[1], "replay");
        const char *command = argc == 2 ? argv[1] : replay ? argv[2] : "";
        if (strcmp(command, "0") && strcmp(command, "180") && strcmp(command, "angle")) {
            fputs("usage: duo_hinge 0|180|angle|replay 0|replay 180|orientation|touch\n", stderr);
            return 2;
        }
        const BOOL readOnly = !strcmp(command, "angle");
        const double target = atof(command);
        Class managerClass = NSClassFromString(@"CMAngleManager");
        if (!managerClass || ![managerClass isAvailable]) {
            fputs("Simulator hinge is unavailable\n", stderr);
            return 1;
        }
        CMAngleManager *manager = [[managerClass alloc] init];
        __block double actual = NAN;
        [manager startAngleUpdatesToQueue:NSOperationQueue.mainQueue handler:^(CMAngle *angle) {
            actual = [angle respondsToSelector:@selector(mechanicalAngleDegrees)]
                ? angle.mechanicalAngleDegrees : angle.angleDegrees;
        }];
        BOOL sent = NO;
        const NSTimeInterval deadline = NSProcessInfo.processInfo.systemUptime + 4.0;
        while (NSProcessInfo.processInfo.systemUptime < deadline) {
            CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0.05, false);
            if (!isfinite(actual)) {
                continue;
            }
            if (readOnly || ((!replay || sent) && fabs(actual - target) <= 0.5)) {
                [manager stopAngleUpdates];
                printf("%.1f\n", actual);
                return 0;
            }
            if (!sent) {
                sent = moveHinge(replay ? 180.0 - target : actual, target);
                if (!sent) {
                    break;
                }
            }
        }
        [manager stopAngleUpdates];
        fprintf(stderr, "Simulator hinge did not reach %.0f degrees (observed %.1f)\n", target, actual);
        return 1;
    }
}
