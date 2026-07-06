pipeline {
    agent any
    stages {
        stage('Checkout') {
            steps { checkout scm }
        }
        stage('Inject .env') {
            steps { sh 'cp /home/jenkins/.env .env' }
        }
        stage('Deploy') {
            steps {
                sh 'docker compose down || true'
                sh 'docker compose build'
                sh 'docker compose up -d'
            }
        }
        stage('Health Check') {
            steps {
                sh '''
                    for i in $(seq 1 12); do
                        if curl -sfkL https://nexus-api.linkworks.in/docs > /dev/null; then
                            echo "API is up"
                            exit 0
                        fi
                        echo "Attempt $i/12 failed, retrying in 5s..."
                        sleep 5
                    done
                    echo "Health check failed after 60s"
                    exit 1
                '''
            }
        }
    }
}
