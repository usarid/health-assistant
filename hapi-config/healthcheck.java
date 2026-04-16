import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;

/**
 * Minimal health check for HAPI FHIR in a distroless container.
 * Runs as a single-file Java source program (Java 11+):
 *   java healthcheck.java
 * Exits 0 if /fhir/metadata returns 200, 1 otherwise.
 */
public class healthcheck {
    public static void main(String[] args) {
        try {
            var client = HttpClient.newBuilder()
                .connectTimeout(Duration.ofSeconds(5))
                .build();
            var request = HttpRequest.newBuilder()
                .uri(URI.create("http://localhost:8080/fhir/metadata"))
                .timeout(Duration.ofSeconds(10))
                .GET()
                .build();
            var response = client.send(request, HttpResponse.BodyHandlers.discarding());
            System.exit(response.statusCode() == 200 ? 0 : 1);
        } catch (Exception e) {
            System.exit(1);
        }
    }
}
