package ai.image.pipeline.config;

import jakarta.jms.XAConnectionFactory;
import org.apache.activemq.artemis.jms.client.ActiveMQXAConnectionFactory;
import org.postgresql.xa.PGXADataSource;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

import javax.sql.XADataSource;

@Configuration
public class JtaConfig {

    @Bean
    public XADataSource xaDataSource(
            @Value("${spring.datasource.url}") String url,
            @Value("${spring.datasource.username}") String username,
            @Value("${spring.datasource.password}") String password) {
        PGXADataSource ds = new PGXADataSource();
        ds.setUrl(url);
        ds.setUser(username);
        ds.setPassword(password);
        return ds;
    }

    @Bean
    public XAConnectionFactory xaConnectionFactory(
            @Value("${spring.artemis.broker-url}") String brokerUrl) {
        return new ActiveMQXAConnectionFactory(brokerUrl);
    }
}
