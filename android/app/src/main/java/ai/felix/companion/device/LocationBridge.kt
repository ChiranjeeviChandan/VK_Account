package ai.felix.companion.device

import ai.felix.companion.core.Errors
import ai.felix.companion.core.Outcome
import android.Manifest
import android.content.Context
import android.location.Location
import android.location.LocationListener
import android.location.LocationManager
import android.os.Build
import android.os.Bundle
import android.os.Looper
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executor
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference
import java.util.function.Consumer

/** Where the phone is, using whatever location providers are switched on. */
object LocationBridge {
    private const val FRESH_MS = 2 * 60 * 1000L
    private const val WAIT_SECONDS = 15L

    fun current(context: Context): Outcome {
        if (!Permissions.location(context)) {
            return Outcome.fail(Errors.PERMISSION_DENIED, "Felix is not allowed to use location.")
        }
        val manager = context.getSystemService(LocationManager::class.java)
            ?: return Outcome.fail(Errors.UNAVAILABLE, "This device has no location service.")
        return try {
            val providers = manager.getProviders(true)
            if (providers.isEmpty()) {
                return Outcome.fail(Errors.UNAVAILABLE, "Location is switched off on the phone.")
            }
            var best: Location? = null
            for (provider in providers) {
                val known = manager.getLastKnownLocation(provider) ?: continue
                if (best == null || known.time > best.time) best = known
            }
            if (best == null || System.currentTimeMillis() - best.time > FRESH_MS) {
                val fresh = freshFix(context, manager, providers)
                if (fresh != null) best = fresh
            }
            val fix = best ?: return Outcome.fail(
                Errors.UNAVAILABLE,
                if (Permissions.backgroundLocation(context)) {
                    "No location fix is available right now."
                } else {
                    "No location fix. Set Felix's location permission to \"Allow all the time\" so it works in the background."
                }
            )
            Outcome.ok(
                "lat" to fix.latitude,
                "lon" to fix.longitude,
                "accuracy" to (if (fix.hasAccuracy()) fix.accuracy.toDouble() else null),
                "ts" to fix.time,
            )
        } catch (_: SecurityException) {
            Outcome.fail(Errors.PERMISSION_DENIED, "Felix is not allowed to use location.")
        } catch (e: IllegalArgumentException) {
            Outcome.fail(Errors.UNAVAILABLE, e.message)
        }
    }

    private fun freshFix(context: Context, manager: LocationManager, providers: List<String>): Location? {
        val fine = Permissions.has(context, Manifest.permission.ACCESS_FINE_LOCATION)
        val provider = when {
            "fused" in providers -> "fused"
            fine && LocationManager.GPS_PROVIDER in providers && LocationManager.NETWORK_PROVIDER !in providers ->
                LocationManager.GPS_PROVIDER
            LocationManager.NETWORK_PROVIDER in providers -> LocationManager.NETWORK_PROVIDER
            fine && LocationManager.GPS_PROVIDER in providers -> LocationManager.GPS_PROVIDER
            else -> return null
        }
        val result = AtomicReference<Location?>(null)
        val done = CountDownLatch(1)
        if (Build.VERSION.SDK_INT >= 30) {
            val direct = Executor { it.run() }
            manager.getCurrentLocation(provider, null, direct, Consumer<Location?> { location ->
                result.set(location)
                done.countDown()
            })
            done.await(WAIT_SECONDS, TimeUnit.SECONDS)
        } else {
            val listener = object : LocationListener {
                override fun onLocationChanged(location: Location) {
                    result.set(location)
                    done.countDown()
                }

                // Abstract before Android 11, so they must be implemented.
                @Deprecated("Deprecated in the platform")
                override fun onStatusChanged(provider: String?, status: Int, extras: Bundle?) = Unit
                override fun onProviderEnabled(provider: String) = Unit
                override fun onProviderDisabled(provider: String) {
                    done.countDown()
                }
            }
            @Suppress("DEPRECATION")
            manager.requestSingleUpdate(provider, listener, Looper.getMainLooper())
            done.await(WAIT_SECONDS, TimeUnit.SECONDS)
            manager.removeUpdates(listener)
        }
        return result.get()
    }
}
