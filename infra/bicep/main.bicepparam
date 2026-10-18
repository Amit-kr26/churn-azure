using './main.bicep'

param baseName = 'churnlearn'
param env = 'dev'
param containerImage = readEnvironmentVariable('CONTAINER_IMAGE')
param alertEmail = readEnvironmentVariable('ALERT_EMAIL', '')
param deployEventSubscription = readEnvironmentVariable('DEPLOY_EVENT_SUB', 'false') == 'true'
param allowedOrigins = readEnvironmentVariable('ALLOWED_ORIGINS', '')
param jwtIssuer = readEnvironmentVariable('JWT_ISSUER', '')
param jwtAudience = readEnvironmentVariable('JWT_AUDIENCE', '')
param jwtJwksUrl = readEnvironmentVariable('JWT_JWKS_URL', '')
