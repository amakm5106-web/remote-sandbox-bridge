package com.example.remotesandbox

import okhttp3.Interceptor
import okhttp3.OkHttpClient
import retrofit2.Retrofit
import retrofit2.converter.moshi.MoshiConverterFactory
import retrofit2.http.Body
import retrofit2.http.GET
import retrofit2.http.POST
import retrofit2.http.Path
import retrofit2.http.Query
import com.squareup.moshi.JsonClass

@JsonClass(generateAdapter = true)
data class RunCodeRequest(val language: String, val script: String)

@JsonClass(generateAdapter = true)
data class BuildApkRequest(val source_url: String)

@JsonClass(generateAdapter = true)
data class DispatchResponse(val run_id: Long, val request_id: String, val status: String)

@JsonClass(generateAdapter = true)
data class RunStatus(
    val run_id: Long,
    val status: String,
    val github_status: String?,
    val conclusion: String?,
    val html_url: String?
)

@JsonClass(generateAdapter = true)
data class Artifact(
    val id: Long,
    val name: String,
    val size_in_bytes: Long?,
    val expired: Boolean,
    val download_url: String?
)

@JsonClass(generateAdapter = true)
data class ArtifactsResponse(val run_id: Long, val artifacts: List<Artifact>)

interface RemoteSandboxApi {
    @POST("api/run-code")
    suspend fun runCode(@Body request: RunCodeRequest): DispatchResponse

    @POST("api/build-apk")
    suspend fun buildApk(@Body request: BuildApkRequest): DispatchResponse

    @GET("api/status/{runId}")
    suspend fun status(@Path("runId") runId: Long): RunStatus

    @GET("api/artifacts/{runId}")
    suspend fun artifacts(
        @Path("runId") runId: Long,
        @Query("include_expired") includeExpired: Boolean = false
    ): ArtifactsResponse
}

fun createRemoteSandboxApi(baseUrl: String, bridgeApiKey: String): RemoteSandboxApi {
    // Keep the API key out of source control. In production obtain it from an
    // Android Keystore-backed app configuration or exchange a short-lived token.
    val auth = Interceptor { chain ->
        chain.proceed(
            chain.request().newBuilder()
                .header("X-API-Key", bridgeApiKey)
                .build()
        )
    }
    val client = OkHttpClient.Builder().addInterceptor(auth).build()
    return Retrofit.Builder()
        .baseUrl(baseUrl.trimEnd('/') + "/")
        .client(client)
        .addConverterFactory(MoshiConverterFactory.create())
        .build()
        .create(RemoteSandboxApi::class.java)
}

// Example from a ViewModel/coroutine:
// val started = api.runCode(RunCodeRequest("python", "print('hello')"))
// var result: RunStatus
// do {
//     delay(2_000)
//     result = api.status(started.run_id)
// } while (result.status == "queued" || result.status == "in_progress")
// if (result.status == "completed" && result.conclusion == "success") {
//     val files = api.artifacts(started.run_id).artifacts
//     // Download the GitHub API artifact URL through your backend, or use an
//     // authenticated OkHttp request with the backend's download proxy.
// }
