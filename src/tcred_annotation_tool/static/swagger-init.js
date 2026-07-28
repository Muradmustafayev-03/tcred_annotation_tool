window.addEventListener("DOMContentLoaded", () => {
  window.SwaggerUIBundle({
    url: "/api/openapi.json",
    dom_id: "#swagger-ui",
    deepLinking: true,
    displayRequestDuration: false,
    docExpansion: "list",
    filter: true,
    persistAuthorization: false,
    supportedSubmitMethods: [],
    tryItOutEnabled: false,
  });
});
